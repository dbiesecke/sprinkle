import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from libsprinkle import rclone


def test_generate_rclone_config(tmp_path):
    sa_dir = tmp_path / "accounts"
    sa_dir.mkdir()

    for i in range(3):
        (sa_dir / f"{i}.json").write_text("{}")

    output = tmp_path / "rclone.conf"
    content = rclone.generate_rclone_config(
        str(sa_dir), str(output), "drive-id", max_accounts=2, start_index=1
    )

    assert output.read_text() == content
    assert content.count("[dst") == 2
    assert "root_folder_id = drive-id" in content

