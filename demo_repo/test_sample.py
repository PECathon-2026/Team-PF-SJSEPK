from sample import greet, message


def test_demo_message_is_present():
    assert isinstance(message, str)
    assert message


def test_greet_returns_personalized_greeting():
    assert greet("Ada") == "Hello, Ada!"
