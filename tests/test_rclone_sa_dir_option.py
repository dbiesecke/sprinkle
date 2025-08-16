from pathlib import Path
import sprinkle


def test_rclone_sa_dir_option(tmp_path):
    sa_dir = tmp_path / "accounts"
    sa_dir.mkdir()
    for i in range(2):
        (sa_dir / f"{i}.json").write_text("{}")
    sprinkle.read_args([
        "--rclone-sa-dir",
        str(sa_dir),
        "--rclone-sa-count",
        "1",
        "--drive-id",
        "drive-id",
        "ls",
    ])
    conf_path = Path(sprinkle.__rclone_conf)
    assert conf_path.exists()
    content = conf_path.read_text()
    assert "root_folder_id = drive-id" in content
    assert content.count("[dst") == 1
    conf_path.unlink()
