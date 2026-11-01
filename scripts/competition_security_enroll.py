"""Offline explicit user enrollment. No preset secret, password, or network."""
import argparse
import getpass
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.bank_v1.security import enroll


def main():
    parser=argparse.ArgumentParser(description='Bind existing authenticator secret and independent password; never overwrite.')
    parser.add_argument('--credential-db',required=True,type=Path)
    args=parser.parse_args()
    password=getpass.getpass('New independent password (12+ characters): ')
    again=getpass.getpass('Repeat password: ')
    if password!=again:
        raise SystemExit('Passwords differ; nothing enrolled.')
    secret=getpass.getpass('Base32 secret provisioned in your independent authenticator (hidden): ')
    enroll(args.credential_db.resolve(),password,secret,provenance='USER_ENROLLED')
    print('Credential enrolled locally. Protect this database; do not add it to source/release. No bank operation was authorized.')


if __name__=='__main__':
    main()
