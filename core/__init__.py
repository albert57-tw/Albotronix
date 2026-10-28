class FeatureDisabled(Exception):
    """Raised from a cog's setup() when its required settings are missing."""


class UserFacingError(Exception):
    """An error whose message is safe and useful to show to the user as-is."""
