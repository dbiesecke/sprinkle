from pathlib import Path
import json
import sprinkle


def service_account(email, key_id):
    return {
        "type": "service_account",
        "project_id": "synthetic-project",
        "private_key_id": key_id,
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake-test-key\n-----END PRIVATE KEY-----\n",
        "client_email": email,
        "client_id": key_id,
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def test_rclone_sa_dir_option(tmp_path):
    sa_dir = tmp_path / "accounts"
    sa_dir.mkdir()
    for i in range(2):
        (sa_dir / f"{i}.json").write_text(json.dumps(service_account(f"{i}@example.test", str(i))))
    sprinkle.read_args([
        "--rclone-sa-dir",
        str(sa_dir),
        "--rclone-sa-count",
        "1",
        "--drive-id",
        "drive-id",
        "ls",
    ])
    sprinkle.configure(None)
    sprinkle.prepare_rclone_sa_config()
    conf_path = Path(sprinkle.__rclone_conf)
    assert conf_path.exists()
    content = conf_path.read_text()
    assert "root_folder_id = drive-id" in content
    assert content.count("[dst") == 1
    conf_path.unlink()
