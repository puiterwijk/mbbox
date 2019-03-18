from .base import BaseComponent


class KojiHub(BaseComponent):
    componentName = "koji_hub"
    serviceName = "koji-hub"

    @property
    def hub_pod_name(self):
        return self.state.oc_get_object_name(
            "pod",
            "app=koji-hub",
        )

    def ensure_mntkoji(self):
        self.logger.debug("Ensuring mntkoji is up")
        self.state.apply_object_from_template(
            "general/mntkoji.yml",
            volumename=self.state.config.get('storage', 'mnt_koji_volname'),
        )

    def create_build(self):
        self.state.apply_object_from_template(
            "general/imagestream.yml",
            imagename="koji-hub",
        )
        self.state.apply_object_from_template(
            "koji_hub/buildconfig.yml",
        )
        return "koji-hub"

    def create(self):
        self.ensure_mntkoji()
        self.state.ca.create_service_cert(
            "koji-hub",
            self.state.config.get('koji_hub', 'public_hostname'),
            # Due to the fact that koji-web cannot access the hub via
            # its service name, we need localhost...
            "localhost",
        )
        self.state.apply_object_from_template(
            "koji_hub/configmap.yml",
        )
        self.state.apply_object_from_template(
            "koji_hub/deploymentconfig.yml",
            replicas=1,
            skipprobes=True,
        )
        self.state.apply_object_from_template(
            "koji_hub/service.yml",
        )
        self.state.apply_object_from_template(
            "koji_hub/route.yml",
            hostname=self.state.config.get('koji_hub', 'public_hostname'),
        )
        self.state.oc_wait_for_deploy("koji-hub")
        self.state.database.ensure_database_exists("koji")
        self._ensure_schema()
        self._ensure_admin_user_exists()
        self._ensure_admin_permissions()

        # Now add the health checks
        self.state.apply_object_from_template(
            "koji_hub/deploymentconfig.yml",
            replicas=1,
            skipprobes=False,
        )
        self.state.oc_wait_for_deploy("koji-hub")

    def _ensure_schema(self):
        self.logger.debug("Ensuring koji schema")
        (retcode, stdout, stderr) = self.state.oc_exec(
            self.hub_pod_name,
            "PGPASSWORD=%s psql -h %s koji %s "
            "</usr/share/doc/koji*/docs/schema.sql" % (
                self.state.database.password,
                self.state.database.hostname,
                self.state.database.username,
            ),
        )
        if "already exists" in stderr:
            self.logger.debug("Schema already existed")
            return False
        self.logger.debug("Retcode: %d, stdout: %s, stderr: %s",
                          retcode, stdout, stderr)
        if retcode != 0:
            raise RuntimeError("Error running command")
        self.logger.info("Created koji schema")
        return True

    def _ensure_admin_user_exists(self):
        self.logger.debug("Ensuring admin user")
        (retcode, _, stderr) = self.state.database.run_query(
            "koji",
            "insert into users (name, status, usertype) values "
            "('%s', 0, 0);" % self.state.config.get('koji_hub',
                                                    'admin_username'),
        )
        if "duplicate key value violates unique constraint" in stderr:
            self.logger.debug("Admin user existed")
            return False
        if retcode != 0:
            raise RuntimeError("Error running command")
        self.logger.info("Admin user created")
        return True

    def _ensure_admin_permissions(self):
        self.logger.debug("Ensuring admin permissions")
        (retcode, _, stderr) = self.state.database.run_query(
            "koji",
            "insert into user_perms (user_id, perm_id, creator_id) "
            "values (1, 1, 1);",
        )
        if "duplicate key value violates unique constraint" in stderr:
            self.logger.debug("Admin permissions existed")
            return False
        if retcode != 0:
            raise RuntimeError("Error running command")
        self.logger.info("Admin permissions created")
        return True

    def ensure_builder_user(self, CN, *arches):
        self.logger.debug("Ensuring builder %s", CN)
        (retcode, stdout, _) = self.state.client.run_koji_command(
            ["add-host", CN] + list(arches),
        )
        if 'is already in the database' in stdout:
            return
        if retcode != 0:
            raise RuntimeError("Error running command")

    def ensure_user(self, CN):
        self.logger.debug("Ensuring koji user %s exists", CN)
        (retcode, _, stderr) = self.state.client.run_koji_command(
            ["add-user", CN]
        )
        if 'user already exists' in stderr:
            return
        if retcode != 0:
            raise RuntimeError("Error running command")

    def ensure_permission(self, CN, perm):
        self.logger.debug("Ensuring koji user %s has perm %s", CN, perm)
        (retcode, _, stderr) = self.state.client.run_koji_command(
            ["grant-permission", perm, CN]
        )
        if 'already has permission' in stderr:
            return
        if retcode != 0:
            raise RuntimeError("Error running command")
