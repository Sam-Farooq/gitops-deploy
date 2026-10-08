# Every target here runs offline. helm and kustomize render locally, and
# nothing reads a kubeconfig or asks for a credential.
HELM        ?= helm
KUSTOMIZE   ?= kustomize
CHART       := charts/service
ENVS        := staging prod
SERVICES    := rag-agent model-serving hybrid-search embedding-worker

.PHONY: help lint template build validate test check tools clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

tools: ## check that helm and kustomize are on PATH
	@command -v $(HELM) >/dev/null || { echo "helm not found"; exit 1; }
	@command -v $(KUSTOMIZE) >/dev/null || { echo "kustomize not found"; exit 1; }
	@$(HELM) version --short && $(KUSTOMIZE) version

lint: ## helm lint --strict, every service in every environment
	@for env in $(ENVS); do for svc in $(SERVICES); do \
	  $(HELM) lint $(CHART) --values envs/$$env/$$svc.yaml --strict \
	    | sed "s|^|$$env/$$svc |"; \
	done; done

template: ## render every chart to rendered/<env>/<service>.yaml
	@for env in $(ENVS); do mkdir -p rendered/$$env; for svc in $(SERVICES); do \
	  $(HELM) template $$svc $(CHART) --values envs/$$env/$$svc.yaml \
	    --namespace platform-$$env > rendered/$$env/$$svc.yaml; \
	  echo "rendered/$$env/$$svc.yaml"; \
	done; done

build: ## render both kustomize overlays to rendered/<env>/platform.yaml
	@for env in $(ENVS); do mkdir -p rendered/$$env; \
	  $(KUSTOMIZE) build overlays/$$env > rendered/$$env/platform.yaml; \
	  echo "rendered/$$env/platform.yaml"; \
	done

validate: ## the rules, the capacity arithmetic and the Argo CD checks
	python -m gitopsvalidate

test: ## the full suite, with a missing binary treated as a failure
	GITOPS_REQUIRE_TOOLS=1 pytest -q

check: lint validate test ## what CI runs
	ruff check .
	ruff format --check .

clean:
	rm -rf rendered .pytest_cache .ruff_cache
