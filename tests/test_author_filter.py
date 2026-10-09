#!/usr/bin/env python3
"""Test cases for G pass author filtering"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from __init__ import is_foreign_turn_author, _looks_like_tool_dump

def test_is_foreign_turn_author():
    """Test bot detection logic."""
    
    # Test 1: None author -> not foreign
    assert is_foreign_turn_author(None) == False
    print("✓ Test 1: None author -> not foreign")
    
    # Test 2: Empty dict -> not foreign
    assert is_foreign_turn_author({}) == False
    print("✓ Test 2: Empty dict -> not foreign")
    
    # Test 3: Bot prefix in id
    assert is_foreign_turn_author({"id": "bot:main"}) == True
    assert is_foreign_turn_author({"id": "bot:origin/main"}) == True
    print("✓ Test 3: Bot prefix in id")
    
    # Test 4: is_bot flag
    assert is_foreign_turn_author({"is_bot": True}) == True
    assert is_foreign_turn_author({"is_bot": False}) == False
    print("✓ Test 4: is_bot flag")
    
    # Test 5: Human author (platform id)
    assert is_foreign_turn_author({"id": "123456789", "name": "User"}) == False
    print("✓ Test 5: Human author -> not foreign")
    
    # Test 6: Missing fields
    assert is_foreign_turn_author({"name": "Bot"}) == False
    print("✓ Test 6: Missing id/is_bot -> not foreign")
    
    print("\nAll G pass tests passed!")

def test_looks_like_tool_dump():
    """Test tool-dump fingerprint detection."""
    
    # Test 1: output key
    assert _looks_like_tool_dump('{"output": "test"}') == True
    print("✓ Test 1: output key detected")
    
    # Test 2: content key
    assert _looks_like_tool_dump('{"content": "test"}') == True
    print("✓ Test 2: content key detected")
    
    # Test 3: error key
    assert _looks_like_tool_dump('{"error": "test"}') == True
    print("✓ Test 3: error key detected")
    
    # Test 4: Normal text
    assert _looks_like_tool_dump("This is a normal fact.") == False
    print("✓ Test 4: Normal text -> not tool dump")
    
    # Test 5: Mid-sentence JSON
    assert _looks_like_tool_dump("See {\"output\": \"x\"} for details") == False
    print("✓ Test 5: Mid-sentence JSON -> not tool dump")
    
    print("\nAll tool-dump tests passed!")

if __name__ == "__main__":
    test_is_foreign_turn_author()
    test_looks_like_tool_dump()
    print("\n✅ G pass TDD: 10/10 PASS")
