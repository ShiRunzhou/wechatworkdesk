"""Run interactively on the server; never pass a password on the command line."""
import argparse
import getpass
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workdesk.cloud import initialize_access

p=argparse.ArgumentParser()
p.add_argument('--config',required=True)
p.add_argument('--origin',required=True)
p.add_argument('--username',default='shiru')
a=p.parse_args()
password=getpass.getpass('网站独立登录密码（至少12字符）：')
if password!=getpass.getpass('再次输入：'):raise SystemExit('两次输入不同，未保存')
initialize_access(a.config,a.origin,a.username,password)
print('网站登录配置已保存；未输出密码。')
