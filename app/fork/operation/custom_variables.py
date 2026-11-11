from app.fork.format_template import check_template_value
from app.models.settings import CustomVariable


def _stored_values(stored) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in stored or []:
        try:
            variable = CustomVariable.model_validate(entry)
        except ValueError:
            continue
        values[variable.key] = variable.value
    return values


def custom_variable_refusal(incoming, stored) -> str | None:
    previous = _stored_values(stored)
    for variable in incoming or []:
        if previous.get(variable.key) == variable.value:
            continue
        try:
            check_template_value(variable.value)
        except ValueError as error:
            return f'Custom variable "{variable.key}": {error}'
    return None


async def refuse_new_unformattable_custom_variables(operation, incoming, stored, db=None) -> None:
    refusal = custom_variable_refusal(incoming, stored)
    if refusal is not None:
        await operation.raise_error(message=refusal, code=422, db=db)
