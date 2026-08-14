ENV ?= dev
REGION := eu-1
IMAGE = app:latest
.PHONY: deploy build test

deploy: build
	kubectl apply -n $(ENV) -f $(MANIFEST)
	echo $(REGION) $(IMAGE)

build:
	go build ./...

test lint:
	go test $(FLAGS) ./...

_stage:
	echo internal $(ENV)

%.o: %.c
	cc -c $<

define template
	echo $(NOT_A_PARAM)
endef
