Project path:
E:\HedgeFund_Binod_v2

Task:
Push the entire project to GitHub properly and verify that everything is committed successfully.

Requirements:

1. Inspect the repository at:
   E:\HedgeFund_Binod_v2

2. Check whether it is already a Git repository:
   git status
   git remote -v

3. If Git is not initialized:
   git init

4. Create a proper .gitignore and exclude:
   - .env
   - .env.*
   - node_modules
   - __pycache__
   - *.pyc
   - venv
   - .venv
   - dist
   - build
   - logs
   - *.log
   - any API keys, secrets, credentials, PATs, tokens, broker credentials, MT5 credentials, and private configuration files

5. Scan the repository for secrets before pushing and report any findings.

6. Commit all project files:
   git add .
   git commit -m "Initial commit"

7. Connect the GitHub remote repository that I will provide.

8. Push all branches and verify the push succeeded:
   git push -u origin main

9. After completion provide:
   - active branch
   - remote URL
   - latest commit hash
   - GitHub repository URL
   - confirmation that push succeeded

10. If the push fails for any reason:
    - diagnose the issue
    - fix it
    - retry
    - provide complete details of what was changed

Do not delete or modify project code unless absolutely necessary.
Show all commands executed and explain any fixes made.