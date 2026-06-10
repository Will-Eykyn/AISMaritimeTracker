IMAGE_NAME = willeykyn/aismaritimetracker

.PHONY: dev run build clean logs

## Run in dev mode (live code, local files visible) 
## 		live code reload (via volume mount)
## 		map written locally
## 		no rebuilds when you edit .py files

# dev:
# 	docker run --rm \
# 		--env-file .env \
# 		-v $(PWD):/app \
# 		-w /app \
# 		$(IMAGE_NAME) \
#  		python ais_maritime_tracker.py

dev:
	docker run --rm --env-file .env \
		-v $(PWD):/app -w /app \
		$(IMAGE_NAME) python -u ais_maritime_tracker.py 


## Build the Docker image
## 	Use this after:
## 		editing requirements.txt
##		changing Dockerfile
build:
	docker build -t $(IMAGE_NAME) .

## Run baked image (production-like)
## 		uses baked-in code
## 		verifies image is portable
run:
	docker run --rm \
		--env-file .env \
		$(IMAGE_NAME)

## Remove dangling images & stopped containers
## 		Cleans up docker junk
clean:
	docker system prune -f

## Show running containers
logs:
	docker ps


help:
	@echo "make dev    - run with live code"
	@echo "make build  - build docker image"
	@echo "make run    - run baked image"
	@echo "make clean  - prune docker"
