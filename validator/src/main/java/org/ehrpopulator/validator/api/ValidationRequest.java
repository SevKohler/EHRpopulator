package org.ehrpopulator.validator.api;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Incoming validation request from the Python agent pipeline.
 */
public class ValidationRequest {

    /** The serialized resource content (JSON string). */
    private String content;

    /**
     * Format of the resource:
     * "FHIR_R4" — FHIR R4 JSON resource or Bundle
     * "OPENEHR_FLAT" — openEHR flat JSON composition
     * "OPENEHR_CANONICAL" — openEHR canonical JSON composition
     */
    private String format;

    /**
     * For FHIR: URL of a StructureDefinition profile to validate against.
     * For openEHR: the template ID (must have been uploaded to EHRbase or provided below).
     * Optional — if absent, validates against base spec only.
     */
    @JsonProperty("profile_url")
    private String profileUrl;

    /**
     * Raw OPT XML content. If provided alongside an openEHR composition,
     * the validator will register this OPT for offline validation without
     * requiring a running EHRbase instance.
     */
    @JsonProperty("opt_xml")
    private String optXml;

    /**
     * Raw FHIR StructureDefinition JSON. If provided alongside a FHIR resource,
     * the validator will register this profile locally before validating.
     */
    @JsonProperty("structure_definition_json")
    private String structureDefinitionJson;

    public String getContent() { return content; }
    public void setContent(String content) { this.content = content; }

    public String getFormat() { return format; }
    public void setFormat(String format) { this.format = format; }

    public String getProfileUrl() { return profileUrl; }
    public void setProfileUrl(String profileUrl) { this.profileUrl = profileUrl; }

    public String getOptXml() { return optXml; }
    public void setOptXml(String optXml) { this.optXml = optXml; }

    public String getStructureDefinitionJson() { return structureDefinitionJson; }
    public void setStructureDefinitionJson(String json) { this.structureDefinitionJson = json; }
}
