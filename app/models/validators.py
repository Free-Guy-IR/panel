class NumericValidatorMixin:
    @staticmethod
    def cast_to_int(v):
        """
        Static method to validate and convert numeric values to integers.

        Args:
            v: Input value to be converted

        Returns:
            int or None: Converted integer value

        Raises:
            ValueError: If the input cannot be converted to an integer
        """
        if v is None:  # Allow None values
            return v
        if isinstance(v, float):  # Allow float to int conversion
            return int(v)
        if isinstance(v, int):  # Allow integers directly
            return v
        raise ValueError("must be an integer or a float, not a string")  # Reject strings


class ListValidator:
    @staticmethod
    def nullable_list(list: list | None, name: str) -> list:
        if list and len(list) < 1:
            raise ValueError(f"you must select at least one {name}")
        return list

    @staticmethod
    def not_null_list(list: list, name: str) -> list:
        if not list or len(list) < 1:
            raise ValueError(f"you must select at least one {name}")
        return list
