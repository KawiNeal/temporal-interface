"""Temporal Control Interface — NiceGUI page.

Layout:  header (title, API health, reload)
         left  : configuration tree, lazily loaded one level at a time
         right : details of the selected configuration and the part it references,
                 plus the status-change control

How to read this file:
  - Module level (top): constants, TreeState, and the live-update broadcast shared by all tabs.
  - build_page(): called once per browser tab. Everything inside it belongs to that one tab.
    It first DEFINES helper functions (nothing runs yet), then builds the layout, then
    starts loading data with a timer.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from nicegui import ui

from .api_client import ApiError, ConfigServiceClient, Configuration, Part

# Colour of each status badge (Quasar colour names).
STATUS_COLORS = {
    "configuration": "indigo", "active": "positive", "draft": "grey-6",
    "experimental": "purple", "deprecated": "warning", "obsolete": "orange-9",
    "retired": "blue-grey", "recalled": "negative",
}
# Tooltip shown when hovering over a status badge.
STATUS_HELP = {
    "configuration": "Top-level configuration definition",
    "active": "Approved for use in builds",
    "draft": "Being designed; not approved",
    "experimental": "Trial use only",
    "deprecated": "Still valid, avoid for new builds",
    "obsolete": "No longer supported",
    "retired": "Removed from service",
    "recalled": "Unsafe; must be pulled from the field",
}

# How each tree row is drawn. This is a Vue template that runs in the browser:
# `props.node` is the node dict we build in describe(), `:attr="..."` binds an attribute
# to a value, and {{ ... }} prints a value. One row = icon, label, caption, status badge.
TREE_HEADER = r"""
<div class="row items-center no-wrap q-gutter-x-sm" style="min-height: 28px">
  <q-icon :name="props.node.icon" size="xs" :color="props.node.placeholder ? 'grey-5' : 'grey-8'" />
  <span class="no-wrap" style="white-space: nowrap" :class="props.node.placeholder ? 'text-grey-6 text-italic' : 'text-weight-medium'">{{ props.node.label }}</span>
  <span class="text-grey-7 text-caption" style="white-space: nowrap">{{ props.node.caption }}</span>
  <q-badge v-if="props.node.status" :color="props.node.color" :label="props.node.status" />
</div>
"""


def status_badge(status: str) -> None:
    ui.badge(status, color=STATUS_COLORS.get(status, "grey")).tooltip(STATUS_HELP.get(status, ""))


@dataclass
class TreeState:
    """Everything the page knows about the configurations loaded so far."""
    nodes: list[dict] = field(default_factory=list)          # roots, passed to ui.tree
    node_by_id: dict[str, dict] = field(default_factory=dict)  # quick lookup of ANY loaded node
    configs: dict[str, Configuration] = field(default_factory=dict)  # the API object behind each node
    parent_of: dict[str, str | None] = field(default_factory=dict)   # child id -> parent id (None for roots)
    loaded: set[str] = field(default_factory=set)            # config ids whose children are fetched
    selected: str | None = None                              # id of the node shown in the details panel


def placeholder(parent_id: str, text: str = "Loading…") -> dict:
    # We don't know whether a node has children until we ask the API, and the tree
    # only draws the ▸ expand arrow for nodes that have children. So every new node
    # gets this fake "Loading…" child; expanding replaces it with the real children
    # (or with an empty list for a leaf, which hides the arrow).
    return {"id": f"{parent_id}::placeholder", "label": text, "icon": "hourglass_empty",
            "placeholder": True}


# ---- live updates between browser tabs --------------------------------------
# Every open page registers a listener here. When one page changes a part, all the
# other pages are told, so no tab keeps showing an out-of-date status.
# This covers changes made through this server only; changes made elsewhere
# (Swagger, other tools) are picked up when a node is selected or the tree reloaded.
# It lives at module level, so it is shared by every tab (all tabs run in one process).
PartListener = Callable[[Part], None]
_part_listeners: set[PartListener] = set()


def broadcast_part_change(part: Part, source: PartListener | None = None) -> None:
    """Tell every open page (except the one that made the change) that a part changed."""
    for listener in list(_part_listeners):  # copy: a tab may close while we loop
        if listener is not source:            # the sender has already updated itself
            listener(part)


async def build_page(api: ConfigServiceClient) -> None:
    # Runs once per browser tab. The helpers below are nested functions ("closures"):
    # they all share this tab's `state`, `api`, `tree`, `details`, ... without passing them around.
    state = TreeState()
    client = ui.context.client  # this browser tab's connection

    # ---- helpers that turn API objects into tree nodes ---------------------

    def describe(node: dict, config: Configuration, part: Part) -> None:
        # Decides what a tree row shows. Used when a node is created AND after a part
        # changes (relabel_part), so labels are always produced the same way.
        if config.is_end_unit:
            node["label"] = f"SN {config.end_unit_serial_no}"      # e.g. "SN 00001"
            node["caption"] = f"{part.part_no} · {part.name}"
            node["icon"] = "precision_manufacturing"
        else:
            node["label"] = f"{part.part_no} {part.name}"          # e.g. "TM-1020 Power Cell"
            node["caption"] = f"× {config.quantity} {part.unit}"   # e.g. "× 4 pcs"
            node["icon"] = "settings"
        node["status"] = part.status
        node["color"] = STATUS_COLORS.get(part.status, "grey")

    async def fetch_parts(configs: list[Configuration]) -> dict[str, Part]:
        # Fetch each DISTINCT part once (a set removes duplicates), all at the same time.
        # e.g. the 20 time machines all use TM-1000, so this is 1 request, not 20.
        uuids = sorted({c.part_uuid for c in configs})
        parts = await asyncio.gather(*(api.get_part(u) for u in uuids))
        return dict(zip(uuids, parts))

    def make_nodes(configs: list[Configuration], parts: dict[str, Part],
                   parent_id: str | None) -> list[dict]:
        # Turn configurations into tree node dicts and record them in this tab's state.
        nodes = []
        for config in configs:
            node = {"id": config.uuid, "children": [placeholder(config.uuid)]}
            describe(node, config, parts[config.part_uuid])
            state.node_by_id[config.uuid] = node
            state.configs[config.uuid] = config
            state.parent_of[config.uuid] = parent_id
            nodes.append(node)
        return nodes

    # ---- tree loading ------------------------------------------------------

    def sync_tree() -> None:
        # NiceGUI copies prop values into observable containers, so in-place edits to
        # our node dicts are not seen; re-assigning pushes the current tree to the browser.
        # (`tree` is created further down. That's fine: a closure looks names up when it
        # RUNS, not when it is defined, and this only runs after the layout is built.)
        tree.props["nodes"] = state.nodes
        tree.update()

    async def load_roots() -> None:
        # Called at startup and by the reload button in the header.
        # Re-run the constructor on the SAME object to empty it. A new TreeState()
        # wouldn't work: every helper above holds a reference to this one.
        state.__init__()  # reset everything
        sync_tree()
        details.refresh()
        status_label.set_text("Loading end units…")
        try:
            roots = await api.get_children()          # no parent -> the end units (time machines)
            parts = await fetch_parts(roots)
        except ApiError as exc:
            # Show what went wrong but keep the page usable (the reload button still works).
            status_label.set_text("Could not load end units.")
            ui.notify(f"Could not load end units — {exc}", type="negative", multi_line=True)
            return
        roots.sort(key=lambda c: c.end_unit_serial_no or "")
        state.nodes.extend(make_nodes(roots, parts, None))
        sync_tree()
        status_label.set_text(f"{len(roots)} time machines")

    async def load_children(config_id: str) -> None:
        # Called the first time a node is expanded: replace its placeholder with real children.
        node = state.node_by_id[config_id]
        try:
            children = await api.get_children(config_id)
            parts = await fetch_parts(children)
        except ApiError as exc:
            # Close the node again and DON'T mark it loaded, so expanding it retries.
            tree.collapse([config_id])
            ui.notify(f"Could not load components — {exc}. Expand again to retry.",
                      type="negative", multi_line=True)
            return
        node["children"] = make_nodes(children, parts, config_id)
        state.loaded.add(config_id)   # never fetch this node's children again
        sync_tree()

    async def on_expand(e) -> None:
        # e.value is the FULL list of expanded node ids, not just the one clicked.
        # Load only those not loaded yet (and skip placeholder ids), in parallel.
        newly_expanded = [i for i in (e.value or []) if i not in state.loaded and i in state.node_by_id]
        await asyncio.gather(*(load_children(i) for i in newly_expanded))

    def on_select(e) -> None:
        # Clicking the selected row again deselects it (e.value is None), and clicking a
        # "Loading…" placeholder gives an id we don't know. In both cases keep the
        # previous selection highlighted and do nothing else.
        if e.value is None or e.value not in state.configs:   # deselect or placeholder click
            tree.select(state.selected)
            return
        state.selected = e.value
        details.refresh()   # redraw the right-hand panel for the new selection

    def relabel_part(part: Part) -> int:
        """Update every loaded node that references this part. Returns how many."""
        # A part can appear in many places (e.g. Power Cell in the Core Engine AND the
        # Safety System), so a status change must update every row that uses it.
        count = 0
        for cid, config in state.configs.items():
            if config.part_uuid == part.uuid:
                describe(state.node_by_id[cid], config, part)
                count += 1
        sync_tree()
        return count

    def path_to(config_id: str) -> list[str]:
        # Walk UP the tree via parent_of until reaching a root, then reverse:
        # gives [root, ..., config_id], used for the breadcrumb.
        path, current = [], config_id
        while current is not None:
            path.append(current)
            current = state.parent_of.get(current)
        return list(reversed(path))

    def is_related(config_id: str, using: list[str]) -> bool:
        """True if config_id uses the part, or is the parent or a child of a node that does.

        Parents and children count because allowable transitions depend on related parts.
        """
        return any(cid == config_id or state.parent_of.get(cid) == config_id
                   or state.parent_of.get(config_id) == cid for cid in using)

    def on_part_changed(part: Part) -> None:
        """Called when another tab changed a part: update this tab if it shows that part."""
        using = [cid for cid, c in state.configs.items() if c.part_uuid == part.uuid]
        if not using:
            return  # this tab hasn't loaded the part, so there is nothing stale to fix
        with client:  # run in this tab's context, so ui.notify reaches the right browser
            relabel_part(part)
            if state.selected and is_related(state.selected, using):
                details.refresh()  # re-fetch status and allowed transitions
            ui.notify(f"{part.part_no} {part.name} was changed to “{part.status}” "
                      f"in another session.", type="info")

    # ---- details panel -----------------------------------------------------

    # @ui.refreshable: calling details.refresh() deletes everything this function drew
    # last time and runs it again. That is how the right-hand panel updates.
    @ui.refreshable
    async def details() -> None:
        if state.selected is None:
            with ui.column().classes("w-full items-center q-pa-xl text-grey-7"):
                ui.icon("account_tree", size="xl")
                ui.label("Select a time machine or component in the tree to see its details.")
                ui.label("Expand ▸ a node to see what it is made of.").classes("text-caption")
            return

        config = state.configs[state.selected]
        try:
            # refresh=True: skip the cache, because another technician may have changed
            # this part. Allowed transitions are always fetched fresh (they depend on
            # related parts).
            part = await api.get_part(config.part_uuid, refresh=True)
            allowed = await api.get_allowable_statuses(config.part_uuid)
        except ApiError as exc:
            # Show the problem inside the panel, with a button to try again.
            with ui.card().classes("w-full bg-red-1"):
                ui.label("Could not load this part.").classes("text-weight-bold")
                ui.label(str(exc))
                ui.button("Retry", icon="refresh", on_click=details.refresh).props("flat")
            return
        relabel_part(part)  # keep tree in sync with the freshest data

        # Breadcrumb: where this configuration sits in its time machine
        with ui.row().classes("items-center q-gutter-x-xs text-caption text-grey-8 wrap"):
            for i, cid in enumerate(path_to(config.uuid)):
                if i:
                    ui.icon("chevron_right", size="xs")
                n = state.node_by_id[cid]
                # c=cid "freezes" the current loop value. Without it, every link would use
                # the LAST cid of the loop. `_` ignores the click-event argument.
                # The (a, b) tuple is just a way to run two calls in one lambda.
                ui.link(n["label"]).classes("cursor-pointer").on(
                    "click", lambda _, c=cid: (tree.select(c), on_select_value(c)))

        # Part card: the shared blueprint (same for every configuration that uses it)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center q-gutter-sm"):
                ui.label(f"{part.part_no}").classes("text-h6")
                ui.label(part.name).classes("text-h6 text-weight-regular")
                status_badge(part.status)
            ui.label("Part definition — shared blueprint").classes("text-caption text-grey-7")
            with ui.grid(columns=2).classes("q-mt-sm gap-y-1 gap-x-6"):
                for key, value in [("Part number", part.part_no), ("Name", part.name),
                                   ("Status", part.status), ("Unit of measure", part.unit),
                                   ("Part UUID", part.uuid)]:
                    ui.label(key).classes("text-grey-7")
                    ui.label(value).classes("font-mono" if key.endswith("UUID") else "")

        # Configuration card: this one specific use of the part (quantity, serial, ...)
        with ui.card().classes("w-full"):
            title = "End unit (complete time machine)" if config.is_end_unit else "Configuration (used in assembly)"
            ui.label(title).classes("text-subtitle1 text-weight-medium")
            with ui.grid(columns=2).classes("gap-y-1 gap-x-6"):
                rows = [("Quantity", f"{config.quantity} {part.unit}")]
                if config.is_end_unit:
                    rows.insert(0, ("Serial number", config.end_unit_serial_no))
                if config.uuid in state.loaded:   # only known once the node has been expanded
                    rows.append(("Direct components", str(len(state.node_by_id[config.uuid]["children"]))))
                rows.append(("Configuration UUID", config.uuid))
                for key, value in rows:
                    ui.label(key).classes("text-grey-7")
                    ui.label(value).classes("font-mono" if key.endswith("UUID") else "")

        # Status change
        with ui.card().classes("w-full"):
            ui.label("Change part status").classes("text-subtitle1 text-weight-medium")
            if not allowed.transitions:
                # e.g. TM-1000 "configuration" may only stay "configuration".
                ui.label(f"No status changes are currently allowed from “{part.status}”.") \
                    .classes("text-grey-8")
            else:
                with ui.row().classes("items-center q-gutter-md"):
                    with ui.row().classes("items-center q-gutter-xs"):
                        ui.label("Current:")
                        status_badge(part.status)
                    ui.icon("arrow_forward")
                    # The dropdown only offers what the service says is allowed right now.
                    choice = ui.select(allowed.transitions, label="New status").classes("min-w-[180px]")
                    # The lambda reads choice.value when the button is CLICKED (not when drawn).
                    # bind_enabled_from: Apply stays disabled until a status is chosen (bool(None) is False).
                    ui.button("Apply", icon="check",
                              on_click=lambda: confirm_status_change(part, choice.value)) \
                        .bind_enabled_from(choice, "value", backward=bool)
                ui.label("Only transitions the service currently allows are listed.") \
                    .classes("text-caption text-grey-7")

        # Where else this part appears (among nodes loaded so far)
        # Only LOADED nodes: the API has no "where used" endpoint.
        others = [cid for cid, c in state.configs.items()
                  if c.part_uuid == part.uuid and cid != config.uuid]
        if others:
            with ui.expansion(f"Same part is used in {len(others)} other loaded configuration(s)",
                              icon="hub").classes("w-full"):
                for cid in others:
                    labels = [state.node_by_id[p]["label"] for p in path_to(cid)]
                    ui.link(" › ".join(labels)).classes("cursor-pointer text-body2").on(
                        "click", lambda _, c=cid: (tree.select(c), on_select_value(c)))

    def on_select_value(config_id: str) -> None:
        # Used by the breadcrumb and "also used in" links. tree.select() only moves the
        # highlight; it does NOT trigger on_select, so update state and redraw here.
        state.selected = config_id
        details.refresh()

    async def confirm_status_change(part: Part, new_status: str | None) -> None:
        if not new_status:
            return   # second safety net: Apply is already disabled with nothing chosen
        usages = sum(1 for c in state.configs.values() if c.part_uuid == part.uuid)
        # Confirmation dialog. Status lives on the PART, so the change affects every
        # configuration that uses it, across the whole fleet. Say so before sending.
        with ui.dialog() as dialog, ui.card():
            ui.label("Confirm status change").classes("text-h6")
            with ui.row().classes("items-center q-gutter-sm"):
                ui.label(f"{part.part_no} {part.name}:")
                status_badge(part.status)
                ui.icon("arrow_forward")
                status_badge(new_status)
            with ui.row().classes("items-start no-wrap bg-amber-1 q-pa-sm rounded-borders"):
                ui.icon("warning", color="warning")
                ui.label(f"This changes the shared part definition. Every configuration that uses "
                         f"{part.part_no} is affected — {usages} in the nodes loaded so far, "
                         f"possibly more across the fleet.").classes("text-body2")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button("Change status", on_click=lambda: dialog.submit(True)).props("color=primary")
        # `await dialog` opens it and PAUSES here until the user clicks a button;
        # it returns the value passed to dialog.submit(): True = change, False = cancel.
        if not await dialog:
            return
        try:
            updated = await api.update_part(part.uuid, status=new_status)
        except ApiError as exc:
            # The server has the final say. If the change was no longer allowed (for example
            # someone changed a related part meanwhile), it answers 400 with what IS allowed now.
            hint = f" Allowed now: {', '.join(exc.allowable_statuses)}." if exc.allowable_statuses else ""
            ui.notify(f"Status not changed — {exc.message}.{hint}", type="negative", multi_line=True)
        else:
            # `else` runs only if the try block succeeded.
            changed = relabel_part(updated)
            broadcast_part_change(updated, source=on_part_changed)  # update other open tabs
            ui.notify(f"{updated.part_no} is now “{updated.status}” ({changed} node(s) updated in view).",
                      type="positive")
        # Always redraw with fresh data: the dropdown now shows moves from the NEW status.
        details.refresh()

    # ---- layout ------------------------------------------------------------
    # Everything above only DEFINED functions. From here on, the page is actually built.
    # `with element:` means "put the elements created inside this block into that element".

    # Header: title, API health chip, reload button
    with ui.header().classes("items-center justify-between bg-indigo-9"):
        with ui.row().classes("items-center q-gutter-sm"):
            ui.icon("hourglass_top", size="md")
            ui.label("Temporal Control Interface").classes("text-h6")
        with ui.row().classes("items-center q-gutter-sm"):
            health = ui.chip("Checking API…", icon="sync").props("dense outline color=white")
            ui.button(icon="refresh", on_click=lambda: load_roots()).props("flat round color=white") \
                .tooltip("Reload from the service")

    # Two columns side by side (they stack on narrow screens: max-md:flex-wrap)
    with ui.row().classes("w-full no-wrap items-start q-gutter-md max-md:flex-wrap"):
        # Left: the configuration tree
        with ui.card().classes("w-full md:w-1/2 md:min-w-[420px]"):
            ui.label("Configurations").classes("text-subtitle1 text-weight-medium")
            status_label = ui.label("").classes("text-caption text-grey-7")
            search = ui.input(placeholder="Filter loaded nodes (serial, part no., name)") \
                .props("dense clearable").classes("w-full")
            with ui.scroll_area().classes("w-full h-[70vh]"):
                # Starts empty; load_roots() fills it once the page is live.
                tree = ui.tree(state.nodes, on_expand=on_expand, on_select=on_select)
                tree.add_slot("default-header", TREE_HEADER)   # use our custom row template
                tree.props("no-transition selected-color=primary")
            # Typing in the box sets the tree's filter; rows whose label doesn't match are hidden.
            search.bind_value_to(tree, "filter")
        # Right: the details panel (shows the "select a node" hint at first)
        with ui.column().classes("w-full md:w-1/2 grow"):
            await details()

    async def check_health() -> None:
        ok = await api.health()
        health.text = "API online" if ok else "API unreachable"
        health.icon = "cloud_done" if ok else "cloud_off"

    async def startup() -> None:
        # Health check and loading the end units run at the same time.
        await asyncio.gather(check_health(), load_roots())

    # Load data AFTER the page is shown: the timer waits until the browser has connected,
    # then runs startup() once. The user sees the layout and "Loading end units…"
    # straight away instead of a blank page while the API answers.
    ui.timer(0, startup, once=True)

    # Join the live-update list; leave it when this tab is closed for good.
    # (on_delete, not on_disconnect: on_disconnect also fires on brief reconnects.)
    _part_listeners.add(on_part_changed)
    client.on_delete(lambda: _part_listeners.discard(on_part_changed))
