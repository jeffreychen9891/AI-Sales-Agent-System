from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from fastagent_v2.worker import cli


def test_worker_isolates_one_event_failure_and_continues_dispatch(
    monkeypatch,
) -> None:
    processor = Mock()
    processor.process_one.side_effect = RuntimeError("poisoned event")
    dispatcher = Mock()
    monkeypatch.setattr(cli, "PointerQueue", Mock(return_value=Mock()))
    monkeypatch.setattr(cli, "Database", Mock(return_value=Mock()))
    monkeypatch.setattr(cli, "InboxProcessor", Mock(return_value=processor))
    monkeypatch.setattr(cli, "OutboxDispatcher", Mock(return_value=dispatcher))
    monkeypatch.setattr(cli, "inference_for_provider", Mock(return_value=Mock()))
    settings = SimpleNamespace(
        queue_redis_url="redis://test",
        worker_database_url="postgresql://test",
        model_provider="mock",
        environment="ci",
        channel_send_url="https://example.test/send",
        # **B1 #12's runner reads this**, and the stub has to carry it: a
        # `SimpleNamespace` models exactly the attributes it is given, so a new
        # setting the loop consults turns into an `AttributeError` here rather
        # than into a missing behaviour. `None` is the honest value -- the
        # tombstone key is absent in this stub, and the loop must then skip the
        # purge runner rather than construct one that would write fingerprints
        # nothing can ever verify.
        purge_fingerprint_key=None,
        # **B2-3's channel selection**, and the same argument as above applies:
        # this stub caught the new setting the moment `run_worker` began reading
        # it, which is exactly what a `SimpleNamespace` is here for. `"sink"` is
        # the honest value -- it is the default every environment that has not
        # opted in still runs, and it keeps this case about the thing it is
        # named after (one poisoned event does not stop dispatch) rather than
        # about provider wiring, which `test_channel_provider_wiring.py` owns.
        channel_provider="sink",
    )

    cli.run_worker(settings, once=True)

    processor.process_one.assert_called_once_with(block_ms=250)
    dispatcher.dispatch_one.assert_called_once()
