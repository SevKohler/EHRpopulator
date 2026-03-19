package org.ehrpopulator.validator.openehr;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.ehrbase.openehr.sdk.webtemplate.builder.WebTemplateBuilder;
import org.ehrbase.openehr.sdk.webtemplate.model.WebTemplate;
import org.openehr.schemas.v1.TemplateDocument;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

/**
 * Converts OPT XML to web template JSON using the EHRbase SDK.
 *
 * Uses:
 *   org.openehr.schemas.v1.TemplateDocument  — parses OPT XML into OPERATIONALTEMPLATE (Archie)
 *   org.ehrbase.openehr.sdk:web-template     — builds the WebTemplate from OPERATIONALTEMPLATE
 *
 * The resulting web template JSON includes:
 *   - Flat composition paths (aqlPath) for every element
 *   - RM types, cardinality (min/max)
 *   - Inline code lists for small local value sets
 *   - Numeric range constraints and allowed units for DV_QUANTITY
 *   - localizedNames and localizedDescriptions per element
 *   - Annotations added in the template designer
 *
 * This is the same format EHRbase serves at:
 *   GET /rest/openehr/v1/definition/template/adl1.4/{templateId}
 *   Accept: application/openehr.wt+json
 */
@Component
public class WebTemplateConverter {

    private static final Logger log = LoggerFactory.getLogger(WebTemplateConverter.class);
    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * Parse OPT XML and return web template as a JSON string.
     *
     * @param optXml raw OPT XML content
     * @return web template JSON string
     * @throws Exception if the OPT is malformed or conversion fails
     */
    public String toWebTemplate(String optXml) throws Exception {
        var opt = TemplateDocument.Factory.parse(
                new ByteArrayInputStream(optXml.getBytes(StandardCharsets.UTF_8))
        ).getTemplate();
        log.debug("Parsed OPT: {}", opt.getTemplateId().getValue());

        WebTemplate webTemplate = new WebTemplateBuilder().build(opt, false);

        return objectMapper.writerWithDefaultPrettyPrinter()
                .writeValueAsString(webTemplate);
    }
}
