"""Initialize model encryption key on the server without displaying it."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

path=Path('/secrets/model.key')
if not path.exists():
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as file:file.write(Fernet.generate_key())
    os.chown(path,10001,10001)
print('Model encryption key ready')
