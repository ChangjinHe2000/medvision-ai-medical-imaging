package com.ppgl.analyze.auth;

import java.sql.PreparedStatement;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.stereotype.Repository;

@Repository
public class UserRepository {

    private final JdbcTemplate jdbcTemplate;

    private final RowMapper<UserAccount> rowMapper = (rs, rowNum) -> new UserAccount(
            rs.getLong("id"),
            rs.getString("username"),
            rs.getString("password_hash"),
            rs.getString("display_name"),
            rs.getString("email"),
            rs.getString("phone"),
            rs.getString("patient_id_card"),
            rs.getString("role"),
            rs.getBoolean("enabled"),
            rs.getTimestamp("created_at").toLocalDateTime()
    );

    public UserRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public boolean hasUsers() {
        Integer count = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM users", Integer.class);
        return count != null && count > 0;
    }

    public boolean existsByUsername(String username) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM users WHERE username = ?",
                Integer.class,
                username
        );
        return count != null && count > 0;
    }

    public boolean existsByEmail(String email) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM users WHERE email = ?",
                Integer.class,
                email
        );
        return count != null && count > 0;
    }

    public boolean existsByPhone(String phone) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM users WHERE phone = ?",
                Integer.class,
                phone
        );
        return count != null && count > 0;
    }

    public boolean existsByPatientIdCard(String patientIdCard) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM users WHERE patient_id_card = ?",
                Integer.class,
                patientIdCard
        );
        return count != null && count > 0;
    }

    public Optional<UserAccount> findByUsername(String username) {
        List<UserAccount> users = jdbcTemplate.query(
                baseSelect() + "WHERE username = ?",
                rowMapper,
                username
        );
        return users.stream().findFirst();
    }

    public Optional<UserAccount> findByUsernameOrPhone(String login) {
        List<UserAccount> users = jdbcTemplate.query(
                baseSelect() + "WHERE username = ? OR phone = ?",
                rowMapper,
                login,
                login
        );
        return users.stream().findFirst();
    }

    public Optional<UserAccount> findById(Long id) {
        List<UserAccount> users = jdbcTemplate.query(
                baseSelect() + "WHERE id = ?",
                rowMapper,
                id
        );
        return users.stream().findFirst();
    }

    public UserAccount create(String username, String passwordHash, String displayName, String email, String phone, String patientIdCard, UserRole role) {
        KeyHolder keyHolder = new GeneratedKeyHolder();
        jdbcTemplate.update(connection -> {
            PreparedStatement statement = connection.prepareStatement(
                    "INSERT INTO users (username, password_hash, display_name, email, phone, patient_id_card, role) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    new String[]{"id"}
            );
            statement.setString(1, username);
            statement.setString(2, passwordHash);
            statement.setString(3, displayName);
            statement.setString(4, email);
            statement.setString(5, phone);
            statement.setString(6, patientIdCard);
            statement.setString(7, role.name());
            return statement;
        }, keyHolder);

        Number id = keyHolder.getKey();
        if (id == null) {
            throw new AuthException("注册失败，请稍后再试");
        }
        return findByUsername(username).orElseThrow(() -> new AuthException("注册失败，请稍后再试"));
    }

    private String baseSelect() {
        return "SELECT id, username, password_hash, display_name, email, phone, patient_id_card, role, enabled, created_at FROM users ";
    }
}
