import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from libsprinkle import rclone, service_accounts


def test_union_config_keeps_old_batches_read_only(tmp_path):
    config = tmp_path / 'rclone.conf'
    rclone.generate_rclone_config_from_files([], str(config), 'drive-root', shuffle=False)
    text = rclone.append_union_config(
        str(config), 'sprinkle_union_test', ['batch2_a', 'batch2_b'], ['batch1_a']
    )
    assert 'create_policy = mfs' in text
    assert 'upstreams = batch2_a: batch2_b: batch1_a::ro' in text
    assert '[sprinkle_union_test]' in config.read_text()


def test_union_runs_resume_active_batch_and_persist_account_order(tmp_path):
    registry = service_accounts.ServiceAccountRegistry(str(tmp_path / 'cache.sqlite3'), str(tmp_path / 'store'))
    run = registry.union_run('/source', 'target:/archive')
    batch = registry.create_union_batch(run['id'], [7, 3, 11])
    again = registry.union_run('/source', 'target:/archive')
    batches = registry.union_batches(again['id'])
    assert again['id'] == run['id']
    assert len(batches) == 1
    assert json.loads(batches[0]['account_ids']) == [7, 3, 11]
    assert batches[0]['status'] == 'active'
    registry.update_union_batch(batch['id'], 'completed')
    assert registry.union_batches(run['id'])[0]['status'] == 'completed'
