import random
import json
import os
import subprocess
import sys
import unittest

from continuum.codec import decode_graph, encode_graph
from continuum.errors import ImageError, UnsupportedObjectError


class GraphCodecTests(unittest.TestCase):
    def test_shared_references_and_cycles_survive(self):
        shared = [1, 2, 3]
        root = {"left": shared, "right": shared}
        root["self"] = root
        restored = decode_graph(encode_graph(root))
        self.assertIs(restored["left"], restored["right"])
        self.assertIs(restored["self"], restored)

    def test_random_state_survives(self):
        generator = random.Random(2026)
        first = generator.random()
        restored = decode_graph(encode_graph(generator))
        self.assertEqual(generator.random(), restored.random())
        self.assertNotEqual(first, restored.random())

    def test_unsupported_object_is_explicit(self):
        with self.assertRaisesRegex(UnsupportedObjectError, "unsupported live object"):
            encode_graph(object())

    def test_malformed_boolean_is_rejected(self):
        with self.assertRaisesRegex(ImageError, "invalid boolean"):
            decode_graph(
                {
                    "codec_version": "0.1",
                    "root": {"t": "bool", "v": "false"},
                    "objects": [],
                }
            )

    def test_duplicate_or_noncanonical_object_id_is_rejected(self):
        with self.assertRaisesRegex(ImageError, "identifiers are not canonical"):
            decode_graph(
                {
                    "codec_version": "0.1",
                    "root": {"t": "ref", "id": 0},
                    "objects": [
                        {"id": 0, "kind": "list", "items": []},
                        {"id": 0, "kind": "list", "items": []},
                    ],
                }
            )

    def test_invalid_reference_is_rejected(self):
        with self.assertRaisesRegex(ImageError, "invalid heap reference"):
            decode_graph(
                {
                    "codec_version": "0.1",
                    "root": {"t": "ref", "id": 5},
                    "objects": [],
                }
            )

    def test_non_object_heap_document_is_rejected(self):
        with self.assertRaisesRegex(ImageError, "unsupported heap codec"):
            decode_graph([])

    def test_duplicate_decoded_dictionary_key_is_rejected(self):
        with self.assertRaisesRegex(ImageError, "duplicate dictionary key"):
            decode_graph(
                {
                    "codec_version": "0.1",
                    "root": {"t": "ref", "id": 0},
                    "objects": [
                        {
                            "id": 0,
                            "kind": "dict",
                            "items": [
                                [{"t": "str", "v": "same"}, {"t": "int", "v": "1"}],
                                [{"t": "str", "v": "same"}, {"t": "int", "v": "2"}],
                            ],
                        }
                    ],
                }
            )

    def test_tuple_reconstructs_as_an_immutable_tuple(self):
        restored = decode_graph(encode_graph({"value": (1, 2, 3)}))
        self.assertIsInstance(restored["value"], tuple)
        with self.assertRaises(TypeError):
            restored["value"][0] = 9

    def test_excessive_graph_nesting_is_rejected(self):
        objects = []
        for index in range(502):
            item = (
                {"t": "ref", "id": index + 1}
                if index < 501
                else {"t": "none"}
            )
            objects.append({"id": index, "kind": "list", "items": [item]})
        with self.assertRaisesRegex(ImageError, "nesting limit"):
            decode_graph(
                {
                    "codec_version": "0.1",
                    "root": {"t": "ref", "id": 0},
                    "objects": objects,
                }
            )

    def test_zero_step_range_is_rejected_as_an_image_error(self):
        with self.assertRaisesRegex(ImageError, "invalid range"):
            decode_graph(
                {
                    "codec_version": "0.1",
                    "root": {"t": "ref", "id": 0},
                    "objects": [
                        {
                            "id": 0,
                            "kind": "range",
                            "start": 0,
                            "stop": 1,
                            "step": 0,
                        }
                    ],
                }
            )

    def test_set_encoding_is_deterministic_across_hash_seeds(self):
        script = (
            "import json;"
            "from continuum.codec import encode_graph;"
            "print(json.dumps(encode_graph({'pear','apple','orange'}),"
            "sort_keys=True,separators=(',',':')))"
        )
        outputs = []
        for seed in ("1", "987654"):
            result = subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
                capture_output=True,
                check=True,
            )
            outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
