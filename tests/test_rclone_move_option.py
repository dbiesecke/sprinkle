import sprinkle

def test_rclone_move_option():
    sprinkle.read_args([
        "--rclone-move",
        "ls",
    ])
    sprinkle.configure(None)
    assert sprinkle.__config["rclone_move"] is True
