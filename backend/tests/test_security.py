from app.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_roundtrip():
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert verify_password("correct-horse-battery-staple", hashed)
    assert not verify_password("wrong-password", hashed)


def test_jwt_roundtrip():
    token = create_access_token(subject="user-123")
    assert decode_access_token(token) == "user-123"


def test_jwt_rejects_tampered_token():
    token = create_access_token(subject="user-123")
    # Tamper a character 5 positions from the end, not the last one or
    # two. Base64url-without-padding has a real edge case at the very end
    # of any segment: a 32-byte HMAC-SHA256 signature encodes to 43
    # characters, but 256 bits isn't a multiple of 6, so the LAST
    # character only carries 4 meaningful bits — the other 2 are
    # discarded on decode. That means multiple distinct characters in
    # that exact position can decode to the identical signature bytes,
    # so tampering only the last character (or two) is not guaranteed to
    # change anything at the byte level even though the string visibly
    # changed — which is exactly what intermittently happened here.
    # Position -5 is safely inside the segment, where every character's
    # full 6 bits are significant, so any change there is guaranteed to
    # change the decoded bytes and invalidate the signature.
    idx = -5
    original_char = token[idx]
    replacement = "a" if original_char != "a" else "b"
    tampered = token[:idx] + replacement + token[idx + 1:]
    assert tampered != token
    assert decode_access_token(tampered) is None
