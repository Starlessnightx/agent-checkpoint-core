"""Provider error classification and explicit, local billing suspension policy."""
import json
import time


class ProviderBillingError(RuntimeError):
    pass


class ProviderTransientError(RuntimeError):
    pass


def provider_error(body, status=None):
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {'message': body}
    error = body.get('error', body) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {'message': str(error)}
    code = str(error.get('code') or error.get('type') or '').lower()
    message = str(error.get('message') or code or 'Unknown provider error')
    if code in {'insufficient_quota', 'insufficient_balance', 'balance_not_enough', 'billing_hard_limit_reached', 'billing_not_active'} or status == 402:
        return ProviderBillingError(message)
    if code in {'service_unavailable', 'server_error', 'rate_limit_exceeded', 'gateway_error'} or status == 429 or status is not None and status >= 500:
        return ProviderTransientError(message)
    return RuntimeError(message)


def wait_for_billing_resume(session, interrupt, sleep=time.sleep):
    """Keep the process alive; no provider calls while awaiting explicit resume."""
    executor = session.tools.context.executor
    directory = session.recorder.path.parent
    marker = directory / 'provider-resume'
    status = directory / 'provider-status.json'
    # Never accept a marker left by an earlier suspension.
    if executor.exists(marker):
        executor.remove_file(marker)
    session.emit('provider_paused', reason='billing', resume_marker=str(marker))
    executor.write_file(status, json.dumps({'status': 'waiting_for_billing', 'resume_marker': str(marker)}).encode())
    while not interrupt():
        if executor.exists(marker):
            executor.remove_file(marker)
            executor.write_file(status, b'{"status":"resuming"}')
            session.emit('provider_resumed', reason='explicit_resume_marker')
            return True
        sleep(1)
    session.emit('provider_pause_cancelled', reason='interrupted')
    return False
