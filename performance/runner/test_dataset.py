import unittest
from pathlib import Path

from performance.runner.config import load_yaml, validate_dataset
from performance.runner.dataset import seed_payload_from_manifest


ROOT = Path(__file__).resolve().parents[1]


def load_seed_payload(path: Path):
    dataset = load_yaml(path)
    validate_dataset(dataset)
    return seed_payload_from_manifest(dataset, path)


class PerformanceDatasetTest(unittest.TestCase):
    def test_large_suite_491_generates_exact_fixed_workload(self):
        payload, test_case_count = load_seed_payload(
            ROOT / "datasets/large-suite-491-v1.yaml"
        )

        self.assertEqual(491, test_case_count)
        self.assertEqual(491, len(payload["testCases"]))
        self.assertEqual(491, len({item["name"] for item in payload["testCases"]}))
        self.assertEqual("perf-001", payload["testCases"][0]["name"])
        self.assertEqual("perf-491", payload["testCases"][-1]["name"])
        self.assertTrue(all(item["expectedAction"] == "ALLOW" for item in payload["testCases"]))
        self.assertTrue(all(item["severity"] == "LOW" for item in payload["testCases"]))
        self.assertTrue(all(item["category"] == "performance" for item in payload["testCases"]))

    def test_existing_http_dataset_contract_is_preserved(self):
        payload, test_case_count = load_seed_payload(ROOT / "datasets/baseline-v1.yaml")

        self.assertEqual(78, test_case_count)
        self.assertEqual(78, len(payload["testCases"]))


if __name__ == "__main__":
    unittest.main()
