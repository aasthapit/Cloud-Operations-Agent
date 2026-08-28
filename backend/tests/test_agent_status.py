"""GET /status payload: what the console rail shows about the config plane.

Covers gap G6 / FR-CFG-3: a refused battery edit keeps the last known good
counts AND names the rejected file, so the surface is not just the log.
"""

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cloudops.agent.a2a_app import ConfigStatus, attach_status_route

ATTESTATION = """
version: 3
battery: health_attestation
checks:
  - { id: nodes-ready, name: Nodes ready, tool: ocp__get_nodes }
  - { id: capacity, name: Capacity headroom, tool: ocp__get_capacity }
"""

APP360 = """
version: 2
battery: app360
sections:
  - section: 1
    title: Executive summary
    source: narrative
  - section: 3
    title: Deployment overview
    source: checks
    checks:
      - { id: workload-availability, name: Workload availability, tool: ocp__get_workloads }
"""


def write_config(root):
    (root / "checks").mkdir(parents=True, exist_ok=True)
    (root / "checks" / "health_attestation.yaml").write_text(ATTESTATION)
    (root / "checks" / "app360.yaml").write_text(APP360)
    return root


class TestConfigStatus:
    async def test_counts_and_clean_state(self, tmp_path):
        status = ConfigStatus(write_config(tmp_path))
        payload = await status.payload()
        assert len(payload["config_version"]) == 7
        assert payload["batteries"]["attestation"] == {"version": 3, "checks": 2}
        assert payload["batteries"]["app360"] == {"version": 2, "sections": 2, "checks": 1}
        assert payload["last_error"] is None

    async def test_rejected_edit_is_named_and_then_cleared(self, tmp_path):
        root = write_config(tmp_path)
        status = ConfigStatus(root)
        await status.payload()

        (root / "checks" / "app360.yaml").write_text("sections: [unclosed\n")
        payload = await status.payload()
        assert payload["last_error"]["file"] == "app360.yaml"
        assert payload["last_error"]["message"]
        # Conversations continue on last known good, so the counts stand.
        assert payload["batteries"]["app360"]["checks"] == 1

        (root / "checks" / "app360.yaml").write_text(APP360)
        assert (await status.payload())["last_error"] is None


class TestStatusRoute:
    def test_appended_route_serves_json(self, tmp_path):
        """The route is appended to the app to_a2a() built, so the A2A
        JSON-RPC root keeps its own handler."""
        app = Starlette()
        attach_status_route(app, write_config(tmp_path))
        with TestClient(app) as client:
            response = client.get("/status")
        assert response.status_code == 200
        body = response.json()
        assert body["batteries"]["attestation"]["checks"] == 2
        assert body["last_error"] is None
