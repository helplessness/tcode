"""Host-only Agent view. Never mutate remote schemas or persisted job payloads."""


def agent_schema(schema):
    if isinstance(schema, list):
        return [agent_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: agent_schema(value) for key, value in schema.items()}
    if isinstance(schema.get('properties'), dict):
        removed = {name for name, field in schema['properties'].items()
                   if isinstance(field, dict) and field.get('x-tmcp-source') in {'runtime', 'deployment'}}
        result['properties'] = {key: value for key, value in result['properties'].items() if key not in removed}
        if 'required' in result:
            result['required'] = [name for name in result['required'] if name not in removed]
    return schema if result == schema else result


def contract_summary(schema):
    if not isinstance(schema, dict):
        return ''
    summary = schema.get('x-tmcp-agent-description')
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    # These are existing transport wrappers, not arbitrary business objects.
    for name in ('input_config', 'input', 'payload'):
        child = schema.get('properties', {}).get(name)
        found = contract_summary(child)
        if found:
            return found
    return ''


def workspace_aware(schema):
    if not isinstance(schema, dict):
        return False
    if schema.get('x-tmcp-workspace-aware') is True:
        return True
    return any(workspace_aware(schema.get('properties', {}).get(name))
               for name in ('input_config', 'input', 'payload'))
