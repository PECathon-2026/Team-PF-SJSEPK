import sample


def test_demo_message_is_present():
    assert isinstance(sample.message, str)
    assert sample.message


def test_greet_returns_personalized_greeting():
    assert sample.greet("Ada") == "Hello, Ada!"
