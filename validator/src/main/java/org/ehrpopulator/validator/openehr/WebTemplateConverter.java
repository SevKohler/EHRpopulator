package org.ehrpopulator.validator.openehr;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.ehrbase.openehr.sdk.webtemplate.parser.OPTParser;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

/**
 * Converts OPT XML to web template JSON using the EHRbase SDK.
 *
 * Uses OPTParser (org.ehrbase.openehr.sdk:web-template) which parses OPT XML
 * via Archie's TemplateDocument and builds a WebTemplate with:
 *   - Flat aqlPaths for every element
 *   - RM types, cardinality (min/max)
 *   - Inline code lists for local value sets
 *   - Numeric range constraints and units for DV_QUANTITY
 *   - localizedNames / localizedDescriptions per element
 *   - Annotations from the template designer
 */
@Component
public class WebTemplateConverter {

    private static final Logger log = LoggerFactory.getLogger(WebTemplateConverter.class);
    private final ObjectMapper objectMapper = new ObjectMapper();

    public String toWebTemplate(String optXml) throws Exception {
        var webTemplate = OPTParser.parse(
                new ByteArrayInputStream(optXml.getBytes(StandardCharsets.UTF_8)));
        log.debug("Parsed OPT: {}", webTemplate.getTemplateId());
        return objectMapper.writerWithDefaultPrettyPrinter().writeValueAsString(webTemplate);
    }
}
