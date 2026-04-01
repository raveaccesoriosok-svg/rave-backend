import os
from dotenv import load_dotenv

load_dotenv()

TN_STORE_ID     = os.getenv("TN_STORE_ID", "5950000")
TN_ACCESS_TOKEN = os.getenv("TN_ACCESS_TOKEN", "")
TN_API_BASE     = "https://api.tiendanube.com/v1"
TN_USER_AGENT   = "RaveAccesorios Backend (rave.accesoriosok@gmail.com)"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")
