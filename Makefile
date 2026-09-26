SHELL := /bin/bash
AWS_REGION ?= us-east-1
AWS_PROFILE ?=
IMAGE_TAG ?= dev
TFVARS ?= terraform.tfvars
AWS_ARGS := $(if $(AWS_PROFILE),--profile $(AWS_PROFILE),) --region $(AWS_REGION)

.PHONY: bootstrap images deploy test lint terraform-check destroy

bootstrap:
	terraform -chdir=deploy/aws/bootstrap init
	terraform -chdir=deploy/aws/bootstrap apply -var="aws_region=$(AWS_REGION)"

images:
	@batch_repo=$$(terraform -chdir=deploy/aws/bootstrap output -raw batch_repository_url); \
	lambda_repo=$$(terraform -chdir=deploy/aws/bootstrap output -raw lambda_repository_url); \
	registry=$${batch_repo%%/*}; \
	aws $(AWS_ARGS) ecr get-login-password | docker login --username AWS --password-stdin $$registry; \
	docker build --platform linux/amd64 -f Dockerfile.batch -t $$batch_repo:$(IMAGE_TAG) .; \
	docker push $$batch_repo:$(IMAGE_TAG); \
	docker build --platform linux/amd64 -f Dockerfile.lambda -t $$lambda_repo:$(IMAGE_TAG) .; \
	docker push $$lambda_repo:$(IMAGE_TAG)

deploy:
	terraform -chdir=deploy/aws/stack init
	terraform -chdir=deploy/aws/stack apply -var-file=$(TFVARS) -var="image_tag=$(IMAGE_TAG)"

test:
	uv run --all-extras pytest -m "not e2e"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src

terraform-check:
	terraform fmt -check -recursive deploy
	terraform -chdir=deploy/aws/bootstrap init -backend=false
	terraform -chdir=deploy/aws/bootstrap validate
	terraform -chdir=deploy/aws/stack init -backend=false
	terraform -chdir=deploy/aws/stack validate

destroy:
	@test "$(CONFIRM_DESTROY)" = "yes" || (echo "Set CONFIRM_DESTROY=yes to destroy the AWS stack and ECR repositories"; exit 1)
	terraform -chdir=deploy/aws/stack destroy -var-file=$(TFVARS) -var="image_tag=$(IMAGE_TAG)"
	terraform -chdir=deploy/aws/bootstrap destroy -var="aws_region=$(AWS_REGION)"
