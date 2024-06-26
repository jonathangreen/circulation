from freezegun.config import configure as fg_configure
from pytest import register_assert_rewrite

register_assert_rewrite("tests.fixtures.database")
register_assert_rewrite("tests.fixtures.files")
register_assert_rewrite("tests.fixtures.vendor_id")

pytest_plugins = [
    "tests.fixtures.announcements",
    "tests.fixtures.api_admin",
    "tests.fixtures.api_axis_files",
    "tests.fixtures.api_bibliotheca_files",
    "tests.fixtures.api_controller",
    "tests.fixtures.api_enki_files",
    "tests.fixtures.api_feedbooks_files",
    "tests.fixtures.api_images_files",
    "tests.fixtures.api_kansas_files",
    "tests.fixtures.api_millenium_files",
    "tests.fixtures.api_novelist_files",
    "tests.fixtures.api_nyt_files",
    "tests.fixtures.api_odl",
    "tests.fixtures.api_onix_files",
    "tests.fixtures.api_opds_dist_files",
    "tests.fixtures.api_opds_files",
    "tests.fixtures.api_overdrive_files",
    "tests.fixtures.api_routes",
    "tests.fixtures.authenticator",
    "tests.fixtures.csv_files",
    "tests.fixtures.database",
    "tests.fixtures.files",
    "tests.fixtures.flask",
    "tests.fixtures.library",
    "tests.fixtures.odl",
    "tests.fixtures.opds2_files",
    "tests.fixtures.opds_files",
    "tests.fixtures.sample_covers",
    "tests.fixtures.search",
    "tests.fixtures.services",
    "tests.fixtures.time",
    "tests.fixtures.tls_server",
    "tests.fixtures.vendor_id",
]

# Make sure if we are using pyinstrument to profile tests, that
# freezegun doesn't interfere with it.
# See: https://github.com/spulec/freezegun#ignore-packages
fg_configure(extend_ignore_list=["pyinstrument"])
