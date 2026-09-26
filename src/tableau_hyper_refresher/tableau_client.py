"""Tableau Server Client operations."""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
from typing import Iterable, List, Literal, Optional

import tableauserverclient as TSC
from tenacity import retry

from .settings import RETRY_KWARGS, get_env
from .tableau_auth import TableauAuth

logger = logging.getLogger(__name__)


def _is_zip_file(path: str) -> bool:
    with open(path, "rb") as file_handle:
        return file_handle.read(2) == b"PK"


class TableauClient:
    """Wrapper for Tableau server authentication and operations."""

    def __init__(self, user_email: Optional[str] = None):
        tableau_region = get_env("TABLEAU_REGION_NAME", "us-west-2")
        tableau_server_url = get_env("TABLEAU_SERVER_URL")
        tableau_secret_name = get_env("TABLEAU_SECRET_NAME")
        tableau_site_id = get_env("TABLEAU_SITE_ID")

        self.auth_client = TableauAuth(
            config_region_name=tableau_region,
            config_server_url=tableau_server_url,
            config_tableau_secret=tableau_secret_name,
            config_site_id=tableau_site_id,
            config_user_email=user_email,
        )
        self.auth_client.sign_in_jwt()
        if self.auth_client.server is None:
            raise RuntimeError("Tableau authentication did not return a server client")
        self.server = self.auth_client.server
        self.task_targets = None

    @retry(**RETRY_KWARGS)
    def get_datasource(self, datasource_id: str) -> TSC.DatasourceItem:
        """Get datasource by ID."""
        ds = self.server.datasources.get_by_id(datasource_id)
        self.server.datasources.populate_connections(ds)
        return ds

    @retry(**RETRY_KWARGS)
    def embed_datasource_connection_credentials(
        self,
        datasource_id: str,
        username: str,
        password: str,
        connection_type: str = "redshift",
        authentication_type: str = "auth-user-pass",
    ) -> List[str]:
        """
        Embed datasource credentials by updating datasource connections through Tableau REST API.

        Returns a list of updated connection IDs
        """
        ds = self.server.datasources.get_by_id(datasource_id)
        self.server.datasources.populate_connections(ds)
        connections = ds.connections or []
        if not connections:
            raise RuntimeError(
                f"No datasource connections found for datasource_id={datasource_id}"
            )

        target_connections = [
            conn
            for conn in connections
            if (conn.connection_type or "").lower() == connection_type.lower()
        ]
        if not target_connections:
            available_types = sorted({conn.connection_type for conn in connections})
            raise RuntimeError(
                f"No '{connection_type}' connection found for datasource_id={datasource_id}. "
                f"Available types: {available_types}"
            )

        updated_connection_ids = []
        for conn in target_connections:
            conn.auth_type = authentication_type
            conn.username = username
            conn.password = password
            conn.embed_password = True
            self.server.datasources.update_connection(ds, conn)
            updated_connection_ids.append(conn.id)

        logger.info(
            "Updated datasource connection credentials for datasource_id=%s, connections=%s",
            datasource_id,
            updated_connection_ids,
        )
        return updated_connection_ids

    @retry(**RETRY_KWARGS)
    def get_datasource_owner_id(self, datasource_id: str) -> str:
        """Get datasource owner ID."""
        ds = self.server.datasources.get_by_id(datasource_id)
        if not ds.owner_id:
            raise RuntimeError(f"Datasource {datasource_id} has no owner_id.")
        return ds.owner_id

    @retry(**RETRY_KWARGS)
    def get_user_email(self, user_id: str) -> Optional[str]:
        """Get Tableau user email by user ID."""
        user = self.server.users.get_by_id(user_id)
        return getattr(user, "email", None)

    @retry(**RETRY_KWARGS)
    def set_datasource_owner(
        self, datasource_id: str, owner_id: str
    ) -> TSC.DatasourceItem:
        """Set datasource owner by ID."""
        ds = self.server.datasources.get_by_id(datasource_id)
        ds.owner_id = owner_id
        return self.server.datasources.update(ds)

    @retry(**RETRY_KWARGS)
    def get_existing_tasks(self):
        """
        Get a list of existing tasks (refreshes of workbooks or datasources that are scheduled)
        """
        req_option = TSC.RequestOptions(pagesize=1000)
        all_tasks, _ = self.server.tasks.get(req_option)
        self.task_targets = {"workbook": [], "datasource": []}
        for task in all_tasks:
            self.task_targets[task.target.type].append(task.target.id)
        return self.task_targets

    def check_scheduled_task(self, resource_type, resource_id) -> bool:
        """
        Check whether a workbook or datasource exists within the list of currently scheduled tasks
        """
        if not self.task_targets:
            self.get_existing_tasks()
        return resource_id in self.task_targets[resource_type]

    @retry(**RETRY_KWARGS)
    def get_filtered_datasources(
        self,
        tags: Optional[Iterable[str]] = ["use_hyper_refresh"],
        tag_mode: Optional[Literal["whitelist", "blacklist"]] = "blacklist",
    ):
        """
        Get a current list of datasources and filter it for whether the datasource
        has extracts, is scheduled, and optionally matches tag rules.

        By default, we blacklist datasources being refreshed via hyper refresh.
        """

        if tag_mode and tag_mode not in {"whitelist", "blacklist"}:
            raise ValueError("tag_mode must be 'whitelist', 'blacklist', or None")

        normalized_tags = {t.lower() for t in tags} if tags else set()

        req_option = TSC.RequestOptions(pagesize=1000)
        all_datasources, _ = self.server.datasources.get(req_option)

        logger.info(f"There are {len(all_datasources)} datasources, wow!")

        def tag_filter(ds) -> bool:
            ds_tags = {tag.lower() for tag in ds.tags}

            # No tag filtering requested
            if not normalized_tags or not tag_mode:
                return True

            if tag_mode == "whitelist":
                return bool(ds_tags & normalized_tags)

            if tag_mode == "blacklist":
                return not bool(ds_tags & normalized_tags)

            return True  # defensive default

        filtered_datasources = [
            ds
            for ds in all_datasources
            if ds.has_extracts
            and self.check_scheduled_task("datasource", ds.id)
            and tag_filter(ds)
        ]

        logger.info(
            f"There are {len(filtered_datasources)} datasources after filtering for the ones that are scheduled to refresh though, wow!"
        )

        self.datasources = filtered_datasources
        return self.datasources

    def get_datasource_updated_at(self, datasource_id: str) -> datetime.datetime:
        """Get the last updated timestamp for a datasource."""
        ds = self.get_datasource(datasource_id)
        return ds.updated_at

    @retry(**RETRY_KWARGS)
    def get_project_id(self, project_name: str) -> Optional[str]:
        """
        Get a Tableau project ID from its name.

        Args:
            project_name: The name of the Tableau project

        Returns:
            The project ID as a string, or None if not found
        """
        req_option = TSC.RequestOptions(pagesize=1000)
        all_projects, _ = self.server.projects.get(req_option)

        for project in all_projects:
            if project.name == project_name:
                return project.id

        logger.info(f"Warning: Project '{project_name}' not found")
        return None

    @retry(**RETRY_KWARGS)
    def get_project_datasource_ids(self, project_id: str) -> List[str]:
        """
        Return a list of datasource IDs for all datasources in the given Tableau project.

        Args:
            project_id: The Tableau project ID to search within

        Returns:
            List of datasource IDs (strings) belonging to that project
        """
        req_option = TSC.RequestOptions(pagesize=1000)
        all_datasources, _ = self.server.datasources.get(req_option)

        project_datasources = [
            ds.id
            for ds in all_datasources
            if getattr(ds, "project_id", None) == project_id
        ]

        logger.info(
            f"Found {len(project_datasources)} datasources in project {project_id}"
        )
        return project_datasources

    @retry(**RETRY_KWARGS)
    def get_datasources_with_tag(
        self, tag: str, case_insensitive: bool = True
    ) -> List[TSC.DatasourceItem]:
        """
        Return datasources that contain the given tag.
        UTILITY METHOD! for testing, not used as part of the app

        Args:
            tag: Tag to match on datasource items
            case_insensitive: If True, match ignoring case
        """
        if not tag:
            raise ValueError("tag must be a non-empty string")

        req_option = TSC.RequestOptions(pagesize=1000)
        all_datasources, _ = self.server.datasources.get(req_option)

        if case_insensitive:
            target = tag.lower()

            def has_tag(ds):
                return any(t.lower() == target for t in ds.tags)

        else:

            def has_tag(ds):
                return tag in ds.tags

        tagged_datasources = [ds for ds in all_datasources if has_tag(ds)]
        logger.info(f"Found {len(tagged_datasources)} datasources with tag '{tag}'")
        return tagged_datasources

    @retry(**RETRY_KWARGS)
    def download_datasource(
        self, datasource_id: str, filepath: str, include_extract: bool = True
    ) -> str:
        """Download datasource to filepath. Returns the full path that was written."""
        return self.server.datasources.download(
            datasource_id=datasource_id,
            filepath=filepath,
            include_extract=include_extract,
        )

    @retry(**RETRY_KWARGS)
    def download_all_datasources_tds_files(self, output_folder: str) -> List[str]:
        """
        Download datasources with scheduled tasks (without extracts), extract .tds files,
        and save them to a folder.

        Args:
            output_folder: Path to the folder where .tds files should be saved

        Returns:
            List of paths to the saved .tds files
        """
        # Create output folder if it doesn't exist
        os.makedirs(output_folder, exist_ok=True)

        # Get datasource IDs from existing tasks
        task_targets = self.get_existing_tasks()
        datasource_ids = sorted(set(task_targets.get("datasource", [])))
        logger.info(
            f"Found {len(datasource_ids)} datasources with scheduled tasks to process"
        )

        saved_tds_files = []

        for datasource_id in datasource_ids:
            try:
                # Get datasource item by ID
                datasource = self.get_datasource(datasource_id)
                logger.info(
                    f"Processing datasource: {datasource.name} (ID: {datasource.id})"
                )

                # Create a temporary directory for this datasource
                with tempfile.TemporaryDirectory() as tmpdir:
                    # Download datasource without extract
                    downloaded_path = self.server.datasources.download(
                        datasource_id,
                        filepath=tmpdir,
                        include_extract=False,
                    )

                    # If download returns None, find the file in tmpdir
                    if not downloaded_path:
                        candidates = [
                            os.path.join(tmpdir, f)
                            for f in os.listdir(tmpdir)
                            if f.lower().endswith((".tds", ".tdsx"))
                        ]
                        if not candidates:
                            logger.info(
                                f"Warning: No .tds/.tdsx file found for {datasource.name}"
                            )
                            continue
                        downloaded_path = candidates[0]

                    # Extract .tds file from .tdsx if needed
                    if downloaded_path.lower().endswith(".tdsx"):
                        from .tds import extract_tds_from_tdsx

                        extract_dir = os.path.join(tmpdir, "extracted")
                        os.makedirs(extract_dir, exist_ok=True)
                        tds_path = extract_tds_from_tdsx(downloaded_path, extract_dir)
                    else:
                        # Already a .tds file
                        tds_path = downloaded_path

                    # Copy .tds file to output folder with a meaningful name
                    # Use datasource ID and name for the filename
                    safe_name = datasource.name.replace("/", "_").replace("\\", "_")
                    output_filename = f"{datasource_id}_{safe_name}.tds"
                    output_path = os.path.join(output_folder, output_filename)

                    shutil.copy2(tds_path, output_path)
                    saved_tds_files.append(output_path)
                    logger.info(f"Saved .tds file to: {output_path}")

            except Exception as e:
                logger.error(f"Error processing datasource_id={datasource_id}: {e}")
                continue

        logger.info(
            f"\nCompleted: Saved {len(saved_tds_files)} .tds files to {output_folder}"
        )
        return saved_tds_files

    @retry(**RETRY_KWARGS)
    def publish_datasource(
        self,
        datasource_item: TSC.DatasourceItem,
        filepath: str,
        mode: TSC.Server.PublishMode = TSC.Server.PublishMode.Overwrite,
    ):
        """Publish datasource to Tableau server."""
        try:
            return self.server.datasources.publish(datasource_item, filepath, mode=mode)
        except TSC.ServerResponseError as e:
            logger.info("url:", e.url)
            logger.info("code:", e.code)
            logger.info("summary:", e.summary)
            logger.info("detail:", e.detail)
            raise

    @retry(**RETRY_KWARGS)
    def publish_datasource_with_embedded_credentials(
        self,
        datasource_item: TSC.DatasourceItem,
        filepath: str,
        username: str,
        password: str,
        mode: TSC.Server.PublishMode = TSC.Server.PublishMode.Overwrite,
    ):
        """Publish datasource and embed connection credentials via Tableau API."""
        connection_credentials = TSC.ConnectionCredentials(
            username, password, embed=True, oauth=False
        )
        datasource_item.connection_credentials = connection_credentials
        return self.server.datasources.publish(
            datasource_item,
            filepath,
            mode=mode,
            connection_credentials=connection_credentials,
        )

    def clone_scheduled_datasources_to_project(
        self,
        target_project_id: str,
        name_suffix: str,
        monday_start_time: datetime.time = datetime.time(8, 0),
    ) -> None:
        """
        Clone scheduled datasources to a new project.
        Only used for testing, TODO: remove testing relevant code to supplement for TabeleauClient?
        """
        task_targets = self.get_existing_tasks()
        datasource_ids = sorted(set(task_targets.get("datasource", [])))
        logger.info(f"Found {len(datasource_ids)} scheduled datasource targets")

        # Monday 8AM schedule interval (per your signature: WeeklyInterval(start_time, *interval_values))
        weekly_interval = TSC.WeeklyInterval(
            monday_start_time,
            TSC.IntervalItem.Day.Monday,
        )

        schedule_item = TSC.ScheduleItem(
            name=f"Monday Morning Extract Refresh - {name_suffix}",
            schedule_type=TSC.ScheduleItem.Type.Extract,
            interval_item=weekly_interval,
            execution_order=TSC.ScheduleItem.ExecutionOrder.Parallel,
            priority=50,
        )
        # schedule_item = self.server.schedules.create(schedule_item)
        logger.info(f"Created schedule: {schedule_item.name} (ID: {schedule_item.id})")

        processed = 0

        for datasource_id in datasource_ids:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    ds_item = self.server.datasources.get_by_id(datasource_id)
                    logger.info(
                        f"\nProcessing datasource: {ds_item.name} (ID: {ds_item.id})"
                    )

                    new_name = f"{ds_item.name}_{name_suffix}"
                    new_ds_item = TSC.DatasourceItem(
                        project_id=target_project_id, name=new_name
                    )

                    # IMPORTANT: download into a directory and use the returned path
                    downloaded_path = self.server.datasources.download(
                        datasource_id,
                        filepath=tmpdir,  # directory, not a filename you invented
                        include_extract=True,
                    )

                    # TSC versions vary: sometimes returns a path string, sometimes returns None.
                    # If it returned None, find the file Tableau wrote in tmpdir.
                    if not downloaded_path:
                        candidates = [
                            os.path.join(tmpdir, f)
                            for f in os.listdir(tmpdir)
                            if f.lower().endswith((".tds", ".tdsx"))
                        ]
                        if len(candidates) != 1:
                            raise RuntimeError(
                                f"Expected 1 downloaded .tds/.tdsx, found: {candidates}"
                            )
                        downloaded_path = candidates[0]

                    logger.info(f"Downloaded datasource file: {downloaded_path}")

                    # Optional sanity check: if extension says .tdsx but it isn't zip, fix extension
                    lower = downloaded_path.lower()
                    if lower.endswith(".tdsx") and not _is_zip_file(downloaded_path):
                        # It's actually a .tds XML file saved with wrong extension (rare but happens if you forced names)
                        fixed = downloaded_path[:-1]  # .tdsx -> .tds
                        os.rename(downloaded_path, fixed)
                        downloaded_path = fixed
                        logger.info(
                            f"Fixed extension (not a zip): now {downloaded_path}"
                        )

                    published_ds = self.server.datasources.publish(
                        new_ds_item,
                        downloaded_path,
                        mode=TSC.Server.PublishMode.CreateNew,
                    )
                    logger.info(
                        f"Published cloned datasource: {new_name} (ID: {published_ds.id})"
                    )

                    # Tag it
                    published_ds.tags.add("temporary")
                    self.server.datasources.update(published_ds)
                    logger.info("Added tag: temporary")

                    # Schedule it (creates an ExtractRefresh task)
                    """self.server.schedules.add_to_schedule(
                        schedule_id=schedule_item.id,
                        datasource=published_ds,
                        task_type=TSC.TaskItem.Type.ExtractRefresh,
                    )
                    logger.info("Scheduled extract refresh: Mondays")"""

                    processed += 1

                except Exception as e:
                    logger.info(f"Error processing datasource_id={datasource_id}: {e}")

        logger.info(
            f"\nCompleted cloning & scheduling {processed} of {len(datasource_ids)} datasources into project {target_project_id}"
        )
