from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tools.amethystd.mithril_candidate import MITHRIL_LIBRARY, MithrilCandidateError
from tools.amethystd.model import FailureClass
from tools.amethystd.store import StateStore
from tools.amethystd.supervisor import Supervisor


class MithrilSupervisorPreflightTests(unittest.TestCase):
    def supervisor(self, root: str) -> Supervisor:
        return Supervisor(
            StateStore(root),
            device_udid="00000000-test-device",
            bundle_id="org.angelauramc.amethyst.AgentDebug",
        )

    def candidate(self, root: str) -> Path:
        path = Path(root) / MITHRIL_LIBRARY
        path.write_bytes(b"candidate")
        return path

    def test_invalid_candidate_is_rejected_before_container_client_is_opened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self.supervisor(tmp)
            candidate = self.candidate(tmp)
            with (
                patch.object(
                    supervisor.device,
                    "pmd3_python_api_available",
                    return_value=True,
                ),
                patch(
                    "tools.amethystd.supervisor.inspect_mithril_candidate",
                    side_effect=MithrilCandidateError("wrong iPhoneOS platform"),
                ),
                patch("tools.amethystd.supervisor.AgentContainerClient") as client,
            ):
                result = asyncio.run(
                    supervisor.stage_payload(str(candidate), "mithril")
                )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["failure"], FailureClass.RUNTIME_ABI_PRECHECK_FAILED.value
        )
        self.assertIn("wrong iPhoneOS platform", result["detail"])
        client.assert_not_called()

    def test_valid_contract_is_persisted_with_candidate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self.supervisor(tmp)
            candidate = self.candidate(tmp)
            contract = {
                "version": 1,
                "kind": "mithril-directmetal-ios",
                "platform": "IOS",
                "architectures": ["arm64"],
                "install_name": "@rpath/libmithril.dylib",
                "required_exports_verified": True,
            }
            manifest = {
                "version": 1,
                "name": "mithril",
                "digest": "a" * 64,
                "files": [
                    {
                        "path": MITHRIL_LIBRARY,
                        "size": len(b"candidate"),
                        "sha256": "b" * 64,
                        "code_signature": {"verified": True},
                    }
                ],
            }
            fake_client = MagicMock()
            fake_client.stage_payload = AsyncMock(return_value=manifest)

            with (
                patch.object(
                    supervisor.device,
                    "pmd3_python_api_available",
                    return_value=True,
                ),
                patch(
                    "tools.amethystd.supervisor.inspect_mithril_candidate",
                    return_value=contract,
                ) as inspect,
                patch(
                    "tools.amethystd.supervisor.AgentContainerClient",
                    return_value=fake_client,
                ) as client,
            ):
                result = asyncio.run(
                    supervisor.stage_payload(str(candidate), "mithril")
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["candidate"]["binary_contract"], contract)
            self.assertEqual(
                supervisor.store.load_candidate("mithril")["binary_contract"],
                contract,
            )
            inspect.assert_called_once_with(candidate.resolve())
            client.assert_called_once_with(
                "00000000-test-device",
                "org.angelauramc.amethyst.AgentDebug",
            )
            fake_client.stage_payload.assert_awaited_once_with(
                candidate.resolve(), "mithril"
            )

    def test_non_mithril_payload_keeps_generic_staging_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = self.supervisor(tmp)
            payload = Path(tmp) / "probe.bin"
            payload.write_bytes(b"probe")
            manifest = {
                "version": 1,
                "name": "probe",
                "digest": "c" * 64,
                "files": [],
            }
            fake_client = MagicMock()
            fake_client.stage_payload = AsyncMock(return_value=manifest)
            with (
                patch.object(
                    supervisor.device,
                    "pmd3_python_api_available",
                    return_value=True,
                ),
                patch(
                    "tools.amethystd.supervisor.inspect_mithril_candidate"
                ) as inspect,
                patch(
                    "tools.amethystd.supervisor.AgentContainerClient",
                    return_value=fake_client,
                ),
            ):
                result = asyncio.run(
                    supervisor.stage_payload(str(payload), "probe")
                )

            self.assertTrue(result["ok"])
            self.assertIsNone(result["candidate"]["binary_contract"])
            inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
