.PHONY: deploy demo destroy

deploy:
	cd infra && npm run build && npx aws-cdk@2 deploy --require-approval never

demo:
	./tools/demo.sh

destroy:
	cd infra && npx aws-cdk@2 destroy --force
