from cogs.voice_cog import apply_pronunciations


def test_apply_pronunciations():
    rules = {"Sawyer": "soy er", "Sawyer Jr": "soy er junior", "C++": "see plus plus"}
    assert apply_pronunciations("sawyer has joined", rules) == "soy er has joined"
    assert apply_pronunciations("Sawyer Jr has left", rules) == "soy er junior has left"
    assert apply_pronunciations("Sawyers has joined", rules) == "Sawyers has joined"
    assert apply_pronunciations("C++ and Sawyer", rules) == "see plus plus and soy er"
    assert apply_pronunciations("Sawyer has joined", {}) == "Sawyer has joined"
