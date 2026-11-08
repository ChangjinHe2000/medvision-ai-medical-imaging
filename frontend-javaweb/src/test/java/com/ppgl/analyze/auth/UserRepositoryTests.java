package com.ppgl.analyze.auth;

import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabase;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseBuilder;
import org.springframework.jdbc.datasource.embedded.EmbeddedDatabaseType;

import static org.junit.jupiter.api.Assertions.*;

class UserRepositoryTests {

    private EmbeddedDatabase database;
    private UserRepository repository;

    @BeforeEach
    void setUp() {
        database = new EmbeddedDatabaseBuilder()
                .setType(EmbeddedDatabaseType.H2)
                .setName("users_" + UUID.randomUUID() + ";MODE=MySQL;DATABASE_TO_LOWER=TRUE")
                .addScript("schema.sql")
                .build();
        repository = new UserRepository(new JdbcTemplate(database));
    }

    @AfterEach
    void tearDown() {
        database.shutdown();
    }

    @Test
    void createReturnsUserWhenDatabaseAlsoGeneratesTimestamps() {
        UserAccount user = repository.create(
                "test_patient", "test-hash", "Test Patient", "patient@example.test",
                "13800000001", "110101199001010011", UserRole.PATIENT
        );

        assertNotNull(user.id());
        assertNotNull(user.createdAt());
        assertEquals("test_patient", user.username());
        assertEquals("test-hash", user.passwordHash());
        assertEquals("Test Patient", user.displayName());
        assertEquals("patient@example.test", user.email());
        assertEquals("13800000001", user.phone());
        assertEquals("110101199001010011", user.patientIdCard());
        assertEquals("PATIENT", user.role());
        assertTrue(user.enabled());
        assertEquals(user, repository.findById(user.id()).orElseThrow());
    }

    @Test
    void successiveUsersHaveDistinctIdsAndAllowOptionalFieldsToBeNull() {
        UserAccount first = repository.create(
                "doctor_one", "test-hash", "Doctor One", null,
                "13800000002", null, UserRole.DOCTOR
        );
        UserAccount second = repository.create(
                "doctor_two", "test-hash", "Doctor Two", null,
                "13800000003", null, UserRole.DOCTOR
        );

        assertNotEquals(first.id(), second.id());
        assertNull(second.email());
        assertNull(second.patientIdCard());
        assertEquals("DOCTOR", second.role());
        assertEquals(first, repository.findByUsername("doctor_one").orElseThrow());
        assertEquals(second, repository.findById(second.id()).orElseThrow());
    }
}
