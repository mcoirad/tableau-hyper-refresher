# Security policy

## Reporting

Do not open a public issue for suspected vulnerabilities or leaked credentials.
Use GitHub's private vulnerability reporting feature for this repository.

This reference project has no guaranteed response time or supported-version
window. Reports should include affected revision, impact, reproduction steps,
and suggested mitigations without including live credentials or customer data.

## Secret handling

The application reads Tableau and Redshift credentials from AWS Secrets Manager.
Secret values must never be placed in Terraform state, environment examples,
logs, test fixtures, or repository history. Rotate any credential immediately if
you suspect it was exposed.

