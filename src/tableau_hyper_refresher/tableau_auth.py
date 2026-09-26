"""Tableau connected-app JWT authentication."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import jwt
import tableauserverclient as TSC

from .secrets import get_json_secret

logger = logging.getLogger(__name__)


class TableauAuth:
    """Authenticate to Tableau with a connected-app JWT."""

    def __init__(
        self,
        config_region_name: str,
        config_server_url: str,
        config_tableau_secret: str,
        config_site_id: str,
        config_user_email: str | None = None,
    ) -> None:
        self.config_region_name = config_region_name
        self.config_server_url = config_server_url
        self.config_tableau_secret = config_tableau_secret
        self.config_site_id = config_site_id
        self.config_user_email = config_user_email
        self.server: TSC.Server | None = None

    def sign_in_jwt(self) -> None:
        credentials = get_json_secret(
            self.config_tableau_secret, self.config_region_name
        )
        required = {"client_id", "secret_id", "secret_value", "user"}
        missing = sorted(required - credentials.keys())
        if missing:
            raise ValueError(
                f"Tableau connected-app secret is missing keys: {', '.join(missing)}"
            )

        scopes = [
            "tableau:content:read",
            "tableau:datasources:create",
            "tableau:datasources:download",
            "tableau:datasources:update",
            "tableau:datasource_tags:delete",
            "tableau:datasource_tags:update",
            "tableau:file_uploads:create",
            "tableau:file_uploads:read",
            "tableau:file_uploads:update",
            "tableau:jobs:read",
            "tableau:tasks:*",
            "tableau:users:read",
            "tableau:views:download",
            "tableau:workbooks:*",
            "tableau:workbooks:download",
        ]
        user = self.config_user_email or credentials["user"]
        now = dt.datetime.now(dt.UTC)
        token = jwt.encode(
            {
                "iss": credentials["client_id"],
                "exp": now + dt.timedelta(minutes=5),
                "jti": str(uuid.uuid4()),
                "aud": "tableau",
                "sub": user,
                "scp": scopes,
            },
            credentials["secret_value"],
            algorithm="HS256",
            headers={
                "kid": credentials["secret_id"],
                "iss": credentials["client_id"],
            },
        )
        auth = TSC.JWTAuth(jwt=token, site_id=self.config_site_id)
        server = TSC.Server(self.config_server_url, use_server_version=True)
        server.auth.sign_in(auth)
        self.server = server
        logger.info("Signed in to Tableau as %s", user)
