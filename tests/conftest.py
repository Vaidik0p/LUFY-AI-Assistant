import os, sys, tempfile
os.environ["LUFY_DATA_DIR"] = tempfile.mkdtemp()
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["GNANI_API_KEY"] = "test-key"
os.environ["OPENAI_API_KEY"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
