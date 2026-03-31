import importlib
import os
import unittest
from unittest import mock


class ConfigDigitalDefaultTests(unittest.TestCase):
    def test_defaults_to_op25_backend_and_service(self):
        with mock.patch.dict(
            os.environ,
            {
                k: v
                for k, v in os.environ.items()
                if k not in {"DIGITAL_BACKEND", "DIGITAL_SERVICE_NAME", "UNIT_DIGITAL", "OP25_SERVICE_NAME"}
            },
            clear=True,
        ):
            config = importlib.import_module("ui.config")
            config = importlib.reload(config)

        self.assertEqual("op25", config.DIGITAL_BACKEND)
        self.assertEqual("scanner-digital-op25", config.DIGITAL_SERVICE_NAME)


if __name__ == "__main__":
    unittest.main()
