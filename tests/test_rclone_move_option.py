import sprinkle

def test_rclone_move_option(tmp_path):
    sprinkle.read_args([
        "--rclone-move",
        "--rclone-env-file",
        str(tmp_path / "rclone.env"),
        "ls",
    ])
    sprinkle.configure(None)
    assert sprinkle.__config["rclone_move"] is True
