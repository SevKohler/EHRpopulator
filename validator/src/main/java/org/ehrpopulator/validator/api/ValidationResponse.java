package org.ehrpopulator.validator.api;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Validation result returned to the Python agent pipeline.
 * The pipeline uses `valid` to decide whether to retry generation,
 * and `issues` to feed error context back into the composer agent prompt.
 */
public class ValidationResponse {

    private boolean valid;

    /** All validation issues. Filter by severity for errors vs warnings. */
    private List<Issue> issues;

    @JsonProperty("issue_count")
    private int issueCount;

    public ValidationResponse(boolean valid, List<Issue> issues) {
        this.valid = valid;
        this.issues = issues;
        this.issueCount = issues != null ? issues.size() : 0;
    }

    public boolean isValid() { return valid; }
    public List<Issue> getIssues() { return issues; }
    public int getIssueCount() { return issueCount; }

    public record Issue(
        /** ERROR, WARNING, INFORMATION */
        String severity,
        /** FHIRPath location or openEHR AQL path */
        String location,
        /** Human-readable error message */
        String message
    ) {}
}
