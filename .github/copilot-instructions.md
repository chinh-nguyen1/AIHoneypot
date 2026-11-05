# AI-WebHoneypot - Project Instructions

## 1. Project Identity

This is a university cybersecurity graduation project.
The project is an AI-powered web honeypot and SOC monitoring system.

Main goals:

- Collect HTTP request logs from a decoy website.
- Distinguish normal traffic from malicious traffic.
- Classify attack types using Machine Learning.
- Estimate threat severity.
- Provide defensive recommendations.
- Display security events on a SOC dashboard.
- Support reproducible deployment on another computer.

## 2. Existing Architecture

The existing project contains:

1_decoy_web/ PHP Web Honeypot
2_log_pipeline/ Log Collection and Processing
3_ai_engine/ Machine Learning and FastAPI
4_database/ MySQL Database
5_frontend_dashboard/ React Dashboard
6_security_pentest/ Controlled Testing Tools

Docker Compose manages the services.

Preserve the existing architecture unless a documented
technical reason justifies changing it.

## 3. Critical Development Rules

- Never rewrite the entire project without approval.
- Never delete existing modules or important files without approval.
- Never modify unrelated modules.
- Work on one approved phase at a time.
- Inspect existing code before editing.
- Reuse existing functions and components when appropriate.
- Do not introduce unnecessary dependencies.
- Do not change database schemas without a migration plan.
- Never overwrite existing datasets or model artifacts without approval.
- Never execute destructive commands without explicit approval.
- Never claim that a feature works without testing it.

## 4. AI and Dataset Requirements

- Support multiple Kaggle datasets with different CSV schemas.
- Normalize all datasets into a consistent format.
- Document source, labels, and preprocessing.
- Separate training, validation and testing data.
- Prevent data leakage and duplicate samples across splits.
- Report Accuracy, Precision, Recall, F1-score,
  confusion matrix and per-class metrics.
- Never fabricate model evaluation results.
- Never guarantee 92% accuracy without experimental evidence.
- Save the complete preprocessing and classification pipeline.
- Record model version and training metadata.

## 5. Hybrid AI Requirements

- Local Machine Learning is the primary classifier.
- Gemini is an optional analysis and advisory component.
- Do not send every log to Gemini unnecessarily.
- Handle timeouts, rate limits and API failures.
- Return a safe fallback when an external API is unavailable.
- Treat confidence scores as model estimates, not certainty.
- Support an Unknown or Needs Review classification.

## 6. Database and Logging

- Use environment variables for credentials.
- Never hardcode API keys or database passwords.
- Use parameterized SQL queries.
- Preserve raw events and analysis results.
- Avoid storing plaintext passwords or unnecessary secrets.
- Use structured JSON logs.
- Handle duplicate events, file rotation and connection errors.

## 7. Frontend Requirements

- Use the existing React architecture.
- Keep the SOC dashboard separate from the decoy website.
- Display attack types, timestamps and severity.
- Show recommendations and classification confidence.
- Provide loading, error and empty states.
- Keep the interface responsive.

## 8. Coding and Testing Rules

Before modifying code:

1. Explain the current implementation.
2. Identify files that need modification.
3. Explain possible side effects.
4. Present an implementation plan.

After modifying code:

1. List all modified and created files.
2. Explain each important change.
3. Run relevant tests.
4. Report errors and unresolved issues.
5. Verify that existing functionality still works.

Do not silently skip failed tests.

## 9. Security and Deployment

- Keep .env and credentials outside version control.
- Provide .env.example without real credentials.
- Keep the honeypot isolated from production systems.
- Do not expose internal database or AI services unnecessarily.
- Make deployment reproducible through Docker Compose.
- Document dataset download and model training steps.

## 10. Communication

Explain important technical decisions in Vietnamese.
Use clear, understandable explanations suitable for a
university cybersecurity graduation project.

Prioritize reliability, correctness, maintainability and
reproducibility over unnecessary complexity or visual effects.
