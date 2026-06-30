#
# Fast Appeal Lambda - Docker Image
# Based on AWS Lambda Python 3.12 base image
#
FROM public.ecr.aws/lambda/python:3.12

# Install system dependencies for PDF processing
# pdfplumber requires poppler-utils
RUN dnf install -y \
    poppler-utils \
    && dnf clean all

# Copy requirements and install Python dependencies
COPY lambda/requirements.txt ${LAMBDA_TASK_ROOT}/lambda/
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/lambda/requirements.txt

# Copy application code
COPY app.py ${LAMBDA_TASK_ROOT}/
COPY config.py ${LAMBDA_TASK_ROOT}/
COPY models.py ${LAMBDA_TASK_ROOT}/
COPY ministries.json ${LAMBDA_TASK_ROOT}/

# Copy modules
COPY api/ ${LAMBDA_TASK_ROOT}/api/
COPY llm/ ${LAMBDA_TASK_ROOT}/llm/
COPY services/ ${LAMBDA_TASK_ROOT}/services/

# Copy templates
COPY templates/ ${LAMBDA_TASK_ROOT}/templates/
COPY appeal_templates/ ${LAMBDA_TASK_ROOT}/appeal_templates/

# Copy static files (frontend UI)
COPY static/ ${LAMBDA_TASK_ROOT}/static/

# Copy lambda handler
COPY lambda/lambda_function.py ${LAMBDA_TASK_ROOT}/lambda/

# Create temp directories
RUN mkdir -p ${LAMBDA_TASK_ROOT}/temp ${LAMBDA_TASK_ROOT}/output ${LAMBDA_TASK_ROOT}/logs

# Set handler
CMD ["lambda.lambda_function.handler"]
