import sample

def test_demo_message_is_present():
    assert sample.message

def test_greeting():
    assert sample.greet("Ada") == "Hello, Ada!"
