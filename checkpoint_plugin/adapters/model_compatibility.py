"""Fresh model configuration validation, independent of storage/deployment."""
from checkpoint_plugin.contracts import canonical, decode
from checkpoint_plugin.errors import CapabilityError

def validate_model(saved,model,session_id,exporter=None):
    export=exporter or (lambda target,identity:target.checkpoint_state(identity))
    current=decode(canonical(export(model,session_id)))
    previous=decode(canonical(saved))
    if type(previous) is not dict or type(current) is not dict:
        raise CapabilityError('portable model configuration required')
    # Cache keys identify historical requests; transport authority is always fresh.
    previous.pop('source_cache_key',None)
    current.pop('source_cache_key',None)
    if canonical(previous)!=canonical(current):
        raise CapabilityError('resolved model configuration differs from the checkpoint')
    return True

def restore_model(saved, model, session_id):
    # Reuse source-discovered capability limits without another discovery call.
    # Identity/generation configuration still has to match fresh deployment.
    configuration = saved.get('configuration', {})
    for key in ('context_window_tokens', 'output_reserve_tokens', 'input_capacity_tokens',
                'provider_max_output_tokens', 'capability_source', 'supports_image_input'):
        if key in configuration:
            setattr(model, key, configuration[key])
    if saved.get('pricing') is not None:
        from harness.model_limits import ModelPricing
        model.pricing = ModelPricing(**saved['pricing'])
    model._capability_discovery_attempted = saved.get('capability_discovery_attempted', False)
    return validate_model(saved, model, session_id)
