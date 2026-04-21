DROP TABLE IF EXISTS silver_github_repos;

CREATE TABLE silver_github_repos (
	repository_name VARCHAR (50),
    stars INT,
	forks INT,
    engagement_ratio NUMERIC,
    created_date DATE,
    last_updated DATE,
	ranked INT
);

INSERT INTO silver_github_repos (
    repository_name, stars, forks, engagement_ratio, created_date, last_updated, ranked
)

SELECT 
    "Repository_Name" AS repository_name,
    "Stars" AS stars,
    "Forks" AS forks,
    ROUND("Stars"::NUMERIC / "Forks", 2) AS engagement_ratio,
    "Created_Date"::DATE AS created_date,
    "Last_Updated"::DATE AS last_updated,
    RANK() OVER (ORDER BY "Stars" DESC) AS rank
FROM bronze_github_repos;
