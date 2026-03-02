import time


def test_pipeline_start_stop(simulator_sdk):
    events = []
    responses = []
    simulator_sdk.on_trigger(lambda e: events.append(e))
    simulator_sdk.on_response(lambda r: responses.append(r))
    simulator_sdk.start()
    time.sleep(1.3)
    simulator_sdk.stop()
    assert len(events) >= 1
    assert len(responses) >= 1
