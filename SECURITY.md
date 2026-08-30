# Security Policy

## Supported Versions

As this project is maintained by an individual with limited resources, security updates will only be provided for the latest major release. We strongly recommend all users always upgrade to the latest version.

| Version | Supported |
| ------- | --------- |
| v2.7.x | ✅ Active support — will receive security updates |
| v2.6.x and below | ❌ No longer supported — please upgrade to the latest version |
| CD v1.5.x | ✅ Active support — will receive security updates |
| CD v1.4.x and below | ❌ No longer supported — please upgrade to the latest version |

**Notes**:
- Devops-Glue and Devops_CD versions must remain compatible; we recommend updating both components simultaneously.
- We recommend all users subscribe to Release notifications for this repository to receive timely security updates.

## Reporting a Vulnerability

We take security issues very seriously. If you discover a potential security vulnerability, please follow the process below to report it.

### How to Report

1. **Preferred method**: Send an email to **jeanslw@qq.com** with the subject line starting with `[SECURITY]`.
2. **Alternative method**: Open an Issue on GitHub Issues, but **clearly mark `[SECURITY]` in the title** and **do not disclose vulnerability details publicly**. We will contact you privately first.

### What to Include

Please include as much of the following information as possible:
- Affected component (Devops-Glue / Devops_CD) and version number
- Brief description of the vulnerability
- Steps to reproduce (if possible, provide code or configuration examples that can reproduce the issue)
- Potential attack scenarios and impact scope
- Your contact information (if you wish to receive a reply)

### Handling Process

1. **Acknowledgment**: We will reply to your email within **48 hours** to confirm receipt of the vulnerability report.
2. **Assessment and Fix**: We will assess the severity of the vulnerability as soon as possible and begin work on a fix. Our goal is to release a patch version within **7-14 days** (timeframe depends on complexity).
3. **Public Disclosure**: After the fix is completed, we will publish a new version and note the fixed security issue in the Release Notes. We will also coordinate with the reporter to confirm whether they wish to be publicly acknowledged.

### Vulnerability Handling Principles

- We will treat every reported security issue with the utmost seriousness.
- We will not publicly disclose vulnerability details until a fix is completed and a new version is released.
- If the vulnerability is accepted, you will be notified after the new version is released; if declined, we will provide a reasonable explanation.

### Security Issues in Dependencies

If your vulnerability report involves a third-party dependency (such as PHP packages, Python libraries, etc.), please also report it to the maintainers of the relevant dependency.

## Recommended Security Practices

For self-hosted users, we recommend:
- Always use the latest version
- Restrict access to the admin panel (`/admin`) to internal networks or VPN
- Use strong passwords and enable API Token authentication
- Regularly audit logs and watch for abnormal operations
- Use MySQL / MariaDB instead of SQLite in production environments (SQLite has limited concurrent write capability)

---

Thank you for helping us make Devops-Glue and Devops_CD safer! 🛡️