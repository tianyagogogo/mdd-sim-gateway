"""The browser must be told which answers it may keep, or an upgrade does not arrive.

The WebUI is one page that names a build, plus files whose names contain a hash of their own
contents. Those two need opposite caching, and saying nothing gets the page wrong: an answer
with a validator but no `Cache-Control` may be reused for a period the client invents, and a
web view -- which has no reload button -- can keep showing the previous build long after the
gateway was upgraded.
"""
import unittest
import unittest.mock

from control.app import main


class CacheHeaderTests(unittest.TestCase):
    def test_the_page_naming_the_build_is_always_revalidated(self):
        self.assertEqual(main.INDEX_CACHE_CONTROL, "no-cache")

    def test_hashed_assets_are_kept_for_as_long_as_the_client_likes(self):
        self.assertIn("immutable", main.ASSET_CACHE_CONTROL)
        self.assertIn("max-age=31536000", main.ASSET_CACHE_CONTROL)

    def test_the_asset_mount_applies_it(self):
        """The mount is what serves /assets, so the header has to come from there."""
        seen = {}

        class _Response:
            headers: dict = {}

        class _Parent:
            def file_response(self, *args, **kwargs):
                response = _Response()
                response.headers = dict(seen)
                return response

        files = main._HashedAssets.__new__(main._HashedAssets)
        with unittest.mock.patch.object(main.StaticFiles, "file_response",
                                        _Parent.file_response, create=True):
            response = main._HashedAssets.file_response(files)
        self.assertEqual(response.headers["Cache-Control"], main.ASSET_CACHE_CONTROL)


if __name__ == "__main__":
    unittest.main()
