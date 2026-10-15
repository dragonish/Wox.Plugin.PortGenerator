import unittest

from wox_plugin import Context, Query, QueryEnv, QueryType, Selection

from src.port_main import MyPlugin


class TestMyPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = MyPlugin()

    def test_query(self):
        results = self.plugin.query(
            Context.new(),
            Query(
                id="test",
                type=QueryType.INPUT,
                env=QueryEnv(),
                raw_query="",
                selection=Selection(),
            ),
        )
        self.assertIsNotNone(results)


if __name__ == "__main__":
    unittest.main()
