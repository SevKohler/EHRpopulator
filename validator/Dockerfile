FROM eclipse-temurin:21-jre-alpine

WORKDIR /app

# Copy the fat JAR built by: mvn package -DskipTests
COPY target/ehr-validator-*.jar app.jar

EXPOSE 8181

ENTRYPOINT ["java", "-jar", "app.jar"]
