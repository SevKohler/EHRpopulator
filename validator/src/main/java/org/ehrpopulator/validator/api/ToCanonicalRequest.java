package org.ehrpopulator.validator.api;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Request body for POST /to-canonical.
 * Converts an EHRbase FLAT JSON composition to canonical openEHR JSON.
 */
public class ToCanonicalRequest {

    /** EHRbase FLAT JSON composition. */
    @JsonProperty("flat_json")
    private String flatJson;

    /**
     * Raw OPT XML used to resolve flat paths.
     * Must be provided unless the OPT was already registered via a previous call.
     */
    @JsonProperty("opt_xml")
    private String optXml;

    /**
     * Explicit template ID — used when the flat JSON does not contain _template_id.
     * The pipeline always passes this to avoid relying on auto-detection.
     */
    @JsonProperty("template_id")
    private String templateId;

    public String getFlatJson() { return flatJson; }
    public void setFlatJson(String flatJson) { this.flatJson = flatJson; }

    public String getOptXml() { return optXml; }
    public void setOptXml(String optXml) { this.optXml = optXml; }

    public String getTemplateId() { return templateId; }
    public void setTemplateId(String templateId) { this.templateId = templateId; }
}
