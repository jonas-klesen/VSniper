#!/usr/bin/env python3
import os
import sys
import hashlib
import base64
import secrets
import string

def generate_sha1_htpasswd(username, password):
    sha = hashlib.sha1(password.encode('utf-8')).digest()
    sha_b64 = base64.b64encode(sha).decode('utf-8')
    return f"{username}:{{SHA}}{sha_b64}"

def generate_secure_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def main():
    print("=" * 60)
    print(" vsniper Traefik Basic Auth Credentials Generator ")
    print("=" * 60)
    
    try:
        username = input("Enter username [default: admin]: ").strip()
        if not username:
            username = "admin"
            
        password = input("Enter password [leave blank for a random secure password]: ").strip()
        is_random = False
        if not password:
            password = generate_secure_password()
            is_random = True
            
        credential_str = generate_sha1_htpasswd(username, password)
        
        print("\nGenerated Credentials:")
        print(f"Username: {username}")
        print(f"Password: {password if is_random else '*' * len(password)}")
        if is_random:
            print(f"Make sure to save this secure password: {password}")
            
        print(f"\nRaw htpasswd line (for BASIC_AUTH_USERS):")
        print(f"{credential_str}")
        print("-" * 60)
        
        # Locate .env file
        script_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(script_dir)
        env_path = os.path.join(repo_root, ".env")
        
        if os.path.exists(env_path):
            confirm = input(f"Would you like to automatically write this to your .env file at {env_path}? [y/N]: ").strip().lower()
            if confirm == 'y':
                with open(env_path, 'r') as f:
                    lines = f.readlines()
                
                updated = False
                new_lines = []
                for line in lines:
                    if line.startswith("BASIC_AUTH_USERS="):
                        new_lines.append(f"BASIC_AUTH_USERS={credential_str}\n")
                        updated = True
                    else:
                        new_lines.append(line)
                        
                if not updated:
                    # If it wasn't there, append it
                    if new_lines and not new_lines[-1].endswith('\n'):
                        new_lines.append('\n')
                    new_lines.append(f"BASIC_AUTH_USERS={credential_str}\n")
                    
                with open(env_path, 'w') as f:
                    f.writelines(new_lines)
                    
                print("\n[SUCCESS] Successfully updated .env with BASIC_AUTH_USERS!")
            else:
                print("\n[INFO] Skipped updating .env file.")
        else:
            print(f"\n[WARNING] .env file not found at {env_path}. Please create a .env file first.")
            
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        sys.exit(1)

if __name__ == "__main__":
    main()
