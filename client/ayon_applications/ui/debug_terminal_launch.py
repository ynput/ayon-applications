import os
from typing import Optional

import ayon_api
from qtpy import QtWidgets, QtCore, QtGui

from ayon_applications import ApplicationsAddon
from ayon_applications.defs import Application
from ayon_applications.manager import ApplicationManager
from ayon_applications.utils import (
    get_applications_for_context,
    get_app_environments_for_context,
)

from ayon_core.style import load_stylesheet, get_app_icon_path
from ayon_core.tools.utils import (
    get_ayon_qt_app,
    get_qt_icon,
    PlaceholderLineEdit,
)

APP_NAME_ROLE = QtCore.Qt.UserRole + 1


class ChooseAppDialog(QtWidgets.QDialog):
    def __init__(
        self,
        addon: ApplicationsAddon,
        applications: list[Application],
        show_variant_name_only: bool,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        icon = QtGui.QIcon(get_app_icon_path())
        self.setWindowIcon(icon)
        self.setWindowTitle("Choose Application")

        title_label = QtWidgets.QLabel("Choose Application", self)

        filter_input = PlaceholderLineEdit(self)
        filter_input.setPlaceholderText("Filter applications..")

        apps_view = QtWidgets.QListView(self)
        apps_model = QtGui.QStandardItemModel()
        app_proxy_model = QtCore.QSortFilterProxyModel(self)
        app_proxy_model.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        app_proxy_model.setSourceModel(apps_model)
        apps_view.setModel(app_proxy_model)

        buttons_widget = QtWidgets.QWidget(self)

        confirm_btn = QtWidgets.QPushButton("Confirm", buttons_widget)
        confirm_btn.setEnabled(False)
        cancel_btn = QtWidgets.QPushButton("Cancel", buttons_widget)

        buttons_layout = QtWidgets.QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(cancel_btn, 0)
        buttons_layout.addWidget(confirm_btn, 0)

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.addWidget(title_label, 0)
        main_layout.addWidget(filter_input, 0)
        main_layout.addWidget(apps_view, 1)
        main_layout.addWidget(buttons_widget, 0)

        selection_model = apps_view.selectionModel()

        filter_input.textChanged.connect(self._on_filter_change)
        apps_view.doubleClicked.connect(self._on_double_click)
        selection_model.selectionChanged.connect(self._on_selection_change)
        confirm_btn.clicked.connect(self._on_confirm_click)
        cancel_btn.clicked.connect(self._on_cancel_click)

        self._addon = addon
        self._show_variant_name_only = show_variant_name_only

        self._title_label = title_label
        self._filter_input = filter_input
        self._apps_view = apps_view
        self._apps_model = apps_model
        self._app_proxy_model = app_proxy_model
        self._confirm_btn = confirm_btn
        self._cancel_btn = cancel_btn

        self._result = None

        self._fill_apps(applications)
        self.resize(430, 540)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.setStyleSheet(load_stylesheet())

    def get_result(self) -> Optional[str]:
        return self._result

    def _fill_apps(self, applications: list[Application]) -> None:
        root_item = self._apps_model.invisibleRootItem()
        root_item.removeRows(0, root_item.rowCount())

        empty_pix = QtGui.QPixmap(128, 128)
        empty_pix.fill(QtCore.Qt.transparent)
        empty_icon = QtGui.QIcon(empty_pix)

        items = []
        icons_by_name = {}
        for app in applications:
            icon_name = app.icon
            if icon_name and icon_name not in icons_by_name:
                icon_url = self._addon.get_app_icon_url(
                    icon_name, server=True
                )
                icons_by_name[icon_name] = get_qt_icon({
                    "type": "url",
                    "url": icon_url,
                })

            icon = icons_by_name.get(icon_name)
            if icon is None:
                icon = empty_icon

            item = QtGui.QStandardItem(
                app.label
                if self._show_variant_name_only
                else app.full_label
            )
            item.setFlags(
                QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
            )
            item.setData(icon, QtCore.Qt.DecorationRole)
            item.setData(app.full_name, APP_NAME_ROLE)
            items.append(item)

        if not items:
            item = QtGui.QStandardItem("< No applications found >")
            item.setFlags(QtCore.Qt.NoItemFlags)
            items.append(item)

        root_item.appendRows(items)
        self._app_proxy_model.sort(0)

    def _on_filter_change(self, text: str) -> None:
        self._app_proxy_model.setFilterFixedString(text)

    def _on_double_click(self, index: QtCore.QModelIndex) -> None:
        if not index.isValid():
            return

        flags = self._apps_view.model().flags(index)
        if not (
            flags & QtCore.Qt.ItemIsEnabled
            and flags & QtCore.Qt.ItemIsSelectable
        ):
            return

        value = index.data(APP_NAME_ROLE)
        if not value:
            return
        self._result = value
        self.accept()

    def _on_selection_change(
        self,
        new_selection: QtCore.QItemSelection,
        _old_selection: QtCore.QItemSelection,
    ) -> None:
        self._confirm_btn.setEnabled(not new_selection.empty())

    def _on_confirm_click(self) -> None:
        indexes = self._apps_view.selectedIndexes()
        for index in indexes:
            value = index.data(APP_NAME_ROLE)
            if value:
                self._result = value
                break

        self.accept()

    def _on_cancel_click(self) -> None:
        self.reject()


def choose_app(
    addon: ApplicationsAddon,
    applications: list[Application],
    show_variant_name_only: bool = False,
) -> Optional[str]:
    window = ChooseAppDialog(
        addon, applications, show_variant_name_only
    )
    window.exec_()
    return window.get_result()


class DebugTerminalStarter:
    def __init__(
        self,
        addon: ApplicationsAddon,
        project_name: str,
        task_id: str,
        app_name: Optional[str],
    ):
        self._addon = addon
        self._project_name = project_name
        self._task_id = task_id
        self._app_name = app_name

        timer = QtCore.QTimer()
        timer.setInterval(1)
        timer.setSingleShot(True)
        timer.start()
        timer.timeout.connect(self.start)

        self._timer = timer

    def start(self) -> None:
        success = False
        try:
            success = self._start()
        finally:
            app = QtWidgets.QApplication.instance()
            app.exit(0 if success else 1)

    def _start(self) -> bool:
        apps_manager: ApplicationManager = (
            self._addon.get_applications_manager()
        )
        # Choose a terminal application
        terminal_applications = list(
            apps_manager.app_groups["terminal"].variants.values()
        )
        print(len(terminal_applications))
        if len(terminal_applications) == 0:
            # This is not reported -> Should be
            print(
                "Missing application variants for terminal application. "
                "Please configure "
                "'ayon+settings://applications/applications/terminal'"
            )
            return False

        if len(terminal_applications) == 1:
            # If only one configured shell application, always use that one
            terminal_app = terminal_applications[0]
            print(
                "Only one terminal application variant is configured. "
                f"Defaulting to {terminal_app.full_label}"
            )

        else:
            app_name = choose_app(
                self._addon,
                terminal_applications,
                show_variant_name_only=True,
            )
            terminal_app = apps_manager.applications.get(app_name)

        if not terminal_app:
            return False

        task_entity = ayon_api.get_task_by_id(
            self._project_name, self._task_id
        )
        folder_entity = ayon_api.get_folder_by_id(
            self._project_name, task_entity["folderId"]
        )

        available_apps = get_applications_for_context(
            self._project_name,
            folder_entity,
            task_entity,
        )
        apps = []
        for app_name in available_apps:
            app = apps_manager.applications.get(app_name)
            if app is not None:
                apps.append(app)

        app_name = self._app_name
        if app_name is None:
            app_name = choose_app(self._addon, apps)

        if not app_name:
            print("Application was not selected. Closing.")
            return False

        app = apps_manager.applications.get(app_name)

        print(f"Retrieving environment for: {app.full_label}..")
        env = get_app_environments_for_context(
            self._project_name,
            folder_entity["path"],
            task_entity["name"],
            app.full_name
        )

        # If an executable is found. Then add the parent folder to PATH
        # just so we can run the application easily from the command line.
        exe = app.find_executable()
        if exe:
            exe_path = exe._realpath()
            folder = os.path.dirname(exe_path)
            print(f"Appending to PATH: {folder}")
            env["PATH"] += os.pathsep + folder

        cwd = env.get("AYON_WORKDIR")
        if cwd:
            print(f"Setting Work Directory: {cwd}")

        launch_context = apps_manager.create_launch_context(
            terminal_app.full_name,
            project_name=self._project_name,
            folder_path=folder_entity["path"],
            task_name=task_entity["name"],
            env=env,
        )
        launch_context.kwargs["cwd"] = cwd
        apps_manager.launch_with_context(launch_context)
        return True


def run_with_debug_terminal(
    addon: ApplicationsAddon,
    project_name: str,
    task_id: str,
    app_name: Optional[str],
):
    app = get_ayon_qt_app()
    app.setQuitOnLastWindowClosed(False)

    _starter = DebugTerminalStarter(
        addon,
        project_name,
        task_id,
        app_name,
    )

    app.exec_()
