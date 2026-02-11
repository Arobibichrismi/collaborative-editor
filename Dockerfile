# Use a base image that includes Python, GCC/G++, and Java (JDK)
FROM ubuntu:20.04

# Set environment variables to prevent interactive prompts during apt install
ENV DEBIAN_FRONTEND=noninteractive

# Update packages and install necessary tools:
# python3 (for execution helper and Python code)
# gcc/g++ (for C/C++)
# openjdk-11-jdk (for Java)
RUN apt-get update && apt-get install -y \
    python3 \
    gcc \
    g++ \
    openjdk-11-jdk \
    coreutils \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# Set Python 3 as the default python command
RUN ln -sf /usr/bin/python3 /usr/bin/python

# Create a working directory inside the container
WORKDIR /code

# Define the image name (used in server.py)
LABEL collabx.sandbox.name="collabx-sandbox"